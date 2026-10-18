"""P1-6 -- reclaim is time-based and independent of how busy the loop is; a hung job is abandoned.

Defects reproduced on the unfixed code:
  * reclaim ran every 50 LOOP ITERATIONS (`reclaim_counter % 50`). A busy worker whose iterations
    each take seconds reclaimed a dead worker's jobs only after minutes, and the cadence changed
    with load;
  * nothing bounded how long one job could run, so a hung call held its pending entry (and the
    only worker) indefinitely.

Time is injected (a fake clock) wherever the cadence is tested, never the wall clock.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.enums import ContactStatus
from app.services.queue.dialer_worker import JobOutcome, process_one_job, reclaim_and_redrive
from app.services.queue.factory import get_queue
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhTriggerResult
from tests.test_cp11_concurrency import _engine, _open_admission, _SpyProvider
from tests.test_cp12c_enqueue_pagination import _campaign, _run, _Session


class _Clock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


def _truncate_all() -> None:
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    _truncate_all()
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "queue_reclaim_idle_ms", 0)
    monkeypatch.setattr(settings, "queue_reclaim_interval_seconds", 15.0)
    monkeypatch.setattr(settings, "queue_reclaim_jitter_ratio", 0.0)
    monkeypatch.setattr(settings, "job_deadline_seconds", 0)
    yield
    _truncate_all()
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    _truncate_all()


# -- the cadence clock ---------------------------------------------------------------------------


def test_reclaim_clock_is_time_based_and_due_at_start():
    from app.services.queue.cadence import ReclaimClock

    clock = _Clock()
    timer = ReclaimClock(15.0, 0.0, clock=clock, rng=lambda: 0.5)
    assert timer.due() is True, "a worker that just started must reclaim straight away"
    assert not any(timer.due() for _ in range(1_000)), "asking often must not make it fire often"
    clock.t += 14.9
    assert timer.due() is False
    clock.t += 0.2
    assert timer.due() is True
    assert timer.due() is False


@pytest.mark.parametrize(("rng", "expected"), [(0.0, 12.0), (0.5, 15.0), (1.0, 18.0)])
def test_reclaim_interval_is_jittered_within_the_ratio(rng, expected):
    from app.services.queue.cadence import ReclaimClock

    clock = _Clock()
    timer = ReclaimClock(15.0, 0.2, clock=clock, rng=lambda: rng)
    assert timer.due() is True
    clock.t += expected - 0.01
    assert timer.due() is False
    clock.t += 0.02
    assert timer.due() is True


# -- configuration -------------------------------------------------------------------------------


def test_the_deadline_must_be_shorter_than_the_reclaim_threshold():
    with pytest.raises(ValidationError, match="JOB_DEADLINE_SECONDS"):
        Settings(_env_file=None, job_deadline_seconds=40, queue_reclaim_idle_ms=30_000)
    with pytest.raises(ValidationError, match="JOB_DEADLINE_SECONDS"):
        Settings(_env_file=None, job_deadline_seconds=30, queue_reclaim_idle_ms=30_000)
    assert Settings(_env_file=None, job_deadline_seconds=0).job_deadline_seconds == 0
    assert Settings(_env_file=None).job_deadline_seconds == 25


@pytest.mark.parametrize(
    ("field", "value"),
    [("queue_reclaim_interval_seconds", 0), ("queue_reclaim_jitter_ratio", 1.5)],
)
def test_cadence_settings_are_validated(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


# -- the deadline --------------------------------------------------------------------------------


def test_the_deadline_interrupts_a_blocking_call_and_restores_the_handler():
    import signal

    from app.services.queue.deadline import JobDeadlineExceeded, job_deadline

    before = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(JobDeadlineExceeded):
        with job_deadline(1):
            time.sleep(30)
    assert time.monotonic() - started < 5
    assert signal.getsignal(signal.SIGALRM) == before
    with job_deadline(0):  # disabled
        time.sleep(0.01)
    with job_deadline(5):  # a call that finishes in time is untouched, and the timer is cleared
        time.sleep(0.01)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_the_deadline_is_a_no_op_outside_the_main_thread():
    from app.services.queue.deadline import job_deadline

    result = []

    def run():
        with job_deadline(1):  # signals can only be handled in the main thread
            time.sleep(0.05)
        result.append("ok")

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert result == ["ok"]


# -- the worker loop ----------------------------------------------------------------------------


def _loop(redis_client, provider, clock):
    from app.worker import WorkerLoop

    return WorkerLoop(
        queue=get_queue(),
        admission=_open_admission(redis_client),
        provider=provider,
        circuit_breaker=CircuitBreaker(redis_client, provider.name),
        recovery_scheduler=get_recovery_scheduler(),
        consumer_name="live-worker",
        session_factory=_Session,
        clock=clock,
        rng=lambda: 0.5,
        block_ms=20,
        heartbeat=lambda: None,
    )


def _strand(campaign_id, n):
    """A dead worker read n jobs and never acked them: n pending entries."""
    queue = get_queue()
    assert _run(campaign_id).enqueued == n
    for _ in range(n):
        assert queue.read_one("dead-worker", 50) is not None


def _attempts(contact_ids) -> int:
    with _Session() as s:
        return s.query(CallAttempt).filter(CallAttempt.contact_id.in_(contact_ids)).count()


def test_an_idle_worker_reclaims_a_dead_workers_jobs_within_the_configured_time(
    redis_client, provider
):
    clock = _Clock()
    spy = _SpyProvider(provider)
    loop = _loop(redis_client, spy, clock)
    loop.step()  # the start-up reclaim finds nothing; the next one is 15 s away

    campaign_id, contact_ids = _campaign(3)
    _strand(campaign_id, 3)

    clock.t += 14.0
    loop.step()
    assert spy.calls == 0, "not due yet: the cadence is time, not iterations"

    clock.t += 1.0
    loop.step()
    assert spy.calls == 3, "reclaimed and redialed once the configured time has passed"
    assert _attempts(contact_ids) == 3


def test_a_busy_worker_reclaims_on_time_not_after_fifty_iterations(redis_client, provider):
    clock = _Clock()

    class SlowSpy(_SpyProvider):
        def create_outbound_call(self, *args, **kwargs):
            clock.t += 10.0  # every dial takes 10 s of (fake) time
            return super().create_outbound_call(*args, **kwargs)

    spy = SlowSpy(provider)
    loop = _loop(redis_client, spy, clock)
    loop.step()  # start-up reclaim: nothing to do

    dead_campaign, dead_contacts = _campaign(1)
    _strand(dead_campaign, 1)
    busy_campaign, busy_contacts = _campaign(12)
    assert _run(busy_campaign).enqueued == 12

    for _ in range(4):  # 4 iterations = 40 s of fake time, far below 50 iterations
        loop.step()

    assert _attempts(dead_contacts) == 1, "the dead worker's job must not wait 50 iterations"
    assert _attempts(busy_contacts) < 12  # and the loop was genuinely busy meanwhile


# -- a hung job ---------------------------------------------------------------------------------


class _HungDograh:
    def __init__(self, hang: bool):
        self.hang = hang
        self.triggers = 0

    def find_runs_for_attempt(self, call_attempt_id, since):
        return []

    def trigger_call(self, *, phone_number, initial_context):
        self.triggers += 1
        if self.hang:
            time.sleep(30)  # a call that never returns
        return DograhTriggerResult(77, "WR-77")


def test_a_hung_job_is_abandoned_within_the_deadline_and_recovered_exactly_once(
    redis_client, provider, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "job_deadline_seconds", 1)
    monkeypatch.setattr(settings, "calling_engine", "dograh")
    campaign_id, (contact_id,) = _campaign(1)
    assert _run(campaign_id).enqueued == 1
    queue = get_queue()
    admission = _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, "mock")

    hung = _HungDograh(hang=True)
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: hung)
    started = time.monotonic()
    with _Session() as db:
        outcome = process_one_job(db, queue, admission, provider, breaker, consumer_name="w1")
        db.commit()
    elapsed = time.monotonic() - started

    assert elapsed < 8, f"the hung call held the worker for {elapsed:.1f}s"
    assert outcome in (JobOutcome.DEADLINE_EXCEEDED, JobOutcome.AMBIGUOUS_RECOVERY)
    assert hung.triggers == 1

    # A healthy worker takes over whatever is still pending. The abandoned attempt is resolved
    # through the normal reconcile path: it is NEVER triggered a second time.
    healthy = _HungDograh(hang=False)
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: healthy)
    with _Session() as db:
        # Right after the abandon the claim is still inside its in-flight window: left pending.
        reclaim_and_redrive(db, queue, admission, provider, breaker, "w2")
    assert healthy.triggers == 0
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 1

    with _Session() as db:  # time passes beyond the in-flight window (Python clock, injected)
        attempt = db.query(CallAttempt).filter_by(contact_id=contact_id).one()
        attempt.started_at = datetime.now(UTC) - timedelta(seconds=120)
        db.commit()
    with _Session() as db:
        reclaim_and_redrive(db, queue, admission, provider, breaker, "w2")
    assert healthy.triggers == 0, "an abandoned claim is reconciled, never triggered again"
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 0
    with _Session() as db:
        assert db.query(CallAttempt).filter_by(contact_id=contact_id).count() == 1
        assert db.get(Contact, contact_id).status != ContactStatus.DIALING

    with _Session() as db:  # and a further reclaim has nothing left to do
        assert reclaim_and_redrive(db, queue, admission, provider, breaker, "w2") == 0
    assert hung.triggers + healthy.triggers == 1
