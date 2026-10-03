"""Checkpoint 09 follow-up -- deterministic failure injection for the
dialer path against REAL PostgreSQL and REAL Redis.

Invariants under test:
  * a job is ACKed only after its outcome is durably committed
  * a transient failure never loses a job and never causes a second dial
  * Dograh has no idempotency key, so an unresolved claim is NEVER re-triggered
    -- it is routed through RecoveryManager like any ambiguous trigger
  * Redis outages fail closed (no admission => no call) and lose no business state
  * retries stay bounded (max_retries=2 => 3 attempts), backed off (30s, 600s)
    and RecoveryManager remains the only retry owner
  * SIGTERM stops new claims but lets the in-flight job finish durably

Tests use real committed rows (a rollback inside process_claimed_job would
otherwise wipe the shared fixture transaction); conftest truncates the test
database once per session.
"""

import itertools
import os
import random
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

import pytest
import redis as redis_lib
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    SuppressionSource,
)
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhErrorCategory,
    DograhTriggerResult,
)

_Session = sessionmaker(bind=create_engine(os.environ["PRIMARY_DB_URL"]))


# -- fixtures / helpers -----------------------------------------------------


_RUN_IDS = itertools.count(7_000_000)  # provider_call_id is globally unique in the DB


class FakeDograh:
    """Counts every trigger -- 'calls' is the number of real phone calls
    that would have been placed."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.run_ids: list[int] = []
        self.runs: list[int] = []  # what Dograh's run listing returns for any attempt
        self.reconcile_error: Exception | None = None
        self.reconcile_calls = 0

    def find_runs_for_attempt(self, call_attempt_id: str, since) -> list[int]:
        self.reconcile_calls += 1
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return list(self.runs)

    def trigger_call(self, *, phone_number: str, initial_context: dict) -> DograhTriggerResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.run_ids.append(next(_RUN_IDS))
        return DograhTriggerResult(self.run_ids[-1], f"run-{self.calls}")


class DeadRedis:
    def __getattr__(self, name):
        raise redis_lib.exceptions.ConnectionError("redis is down")


@pytest.fixture
def dograh(monkeypatch):
    """calling_engine=dograh with a counting fake client; no sleeping on
    admission backpressure."""
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    fake = FakeDograh()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: fake)
    monkeypatch.setattr("app.services.queue.dialer_worker.time.sleep", lambda _s: None)
    yield fake
    get_settings.cache_clear()


def _world(n: int = 1, *, window=(dtime(0, 0), dtime.max)) -> tuple[str, list[str]]:
    with _Session() as s:
        campaign = Campaign(name="failure injection", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        s.add(
            RetryPolicy(campaign_id=campaign.id, window_start=window[0], window_end=window[1])
        )
        ids = []
        for _ in range(n):
            phone = f"555-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
            contact = Contact(
                campaign_id=campaign.id,
                phone_number=phone,
                normalized_phone_number=normalize_phone_number(phone),
                status=ContactStatus.PENDING,
            )
            s.add(contact)
            s.flush()
            ids.append(str(contact.id))
        s.commit()
        return str(campaign.id), ids


def _admission(redis_client) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=100_000,
        campaign_cps_limit=100_000,
        provider_cps_limit=100_000,
        global_concurrency_limit=100_000,
        campaign_concurrency_limit=100_000,
        provider_concurrency_limit=100_000,
    )


def _queue(redis_client, name: str) -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, f"test:fi:{name}", f"test:fi:{name}:g")


def _pending(queue: RedisStreamQueue) -> int:
    return queue.redis.xpending(queue.stream_key, queue.group)["pending"]


def _attempts(contact_id: str) -> list[CallAttempt]:
    with _Session() as s:
        return list(
            s.execute(
                select(CallAttempt)
                .where(CallAttempt.contact_id == contact_id)
                .order_by(CallAttempt.attempt_number)
            ).scalars()
        )


def _drive(queue, admission, provider, redis_client, message_id, job):
    with _Session() as db:
        return process_claimed_job(
            db, queue, admission, provider, CircuitBreaker(redis_client, "dograh"), message_id, job
        )


def _transient_db_error() -> OperationalError:
    return OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))


# -- 2A. PostgreSQL transient failure -----------------------------------------


def test_transient_db_failure_before_persistence_loses_no_job_and_dials_once(
    dograh, redis_client, provider, monkeypatch
):
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "db1"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)

    real, state = CallAttemptRepository.get_or_create, {"n": 0}

    def flaky(self, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise _transient_db_error()
        return real(self, *a, **k)

    monkeypatch.setattr(CallAttemptRepository, "get_or_create", flaky)

    with pytest.raises(OperationalError):
        _drive(queue, admission, provider, redis_client, message_id, job)

    assert _pending(queue) == 1  # NOT acked: the job survives the DB failure
    assert _attempts(contact_id) == [] and dograh.calls == 0
    assert redis_client.get("concurrency:global") == "0"  # admission slot not leaked

    # DB recovered: the stale job is reclaimed and processed normally.
    (reclaimed,) = queue.reclaim_stale("w2", idle_ms=0)
    assert _drive(queue, admission, provider, redis_client, *reclaimed) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    assert _pending(queue) == 0 and dograh.calls == 1
    (attempt,) = _attempts(contact_id)
    assert attempt.attempt_number == 1 and attempt.provider_call_id == str(dograh.run_ids[0])


def test_commit_failure_after_trigger_never_causes_a_second_dial(
    dograh, redis_client, provider, monkeypatch
):
    """The nastiest ordering: Dograh accepted the trigger (a real call is
    ringing) and then PostgreSQL fails the final commit. The job must not be
    acked, and recovery must NOT re-trigger."""
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "db2"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)

    with _Session() as db:
        real_commit, n = db.commit, {"c": 0}

        def flaky_commit():
            n["c"] += 1
            if n["c"] == 2:  # 1st = durable pre-trigger claim, 2nd = final outcome
                raise _transient_db_error()
            real_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        with pytest.raises(OperationalError):
            process_claimed_job(
                db, queue, admission, provider, CircuitBreaker(redis_client, "dograh"),
                message_id, job,
            )

    assert dograh.calls == 1 and _pending(queue) == 1  # call placed, job NOT acked
    (attempt,) = _attempts(contact_id)  # the pre-trigger claim survived
    assert attempt.state == CallAttemptState.INITIATED and attempt.provider_call_id is None

    # Reclaimed immediately: the claim is younger than the Dograh request
    # timeouts, so it may still be in flight -> left unacked, never re-triggered.
    (reclaimed,) = queue.reclaim_stale("w2", idle_ms=0)
    assert _drive(queue, admission, provider, redis_client, *reclaimed) == JobOutcome.IN_FLIGHT
    assert dograh.calls == 1 and _pending(queue) == 1

    # Long after any request could still be in flight: ambiguous -> recovery.
    with _Session() as s:
        s.execute(text("UPDATE call_attempt SET started_at = now() - interval '1 hour'"))
        s.commit()
    (reclaimed,) = queue.reclaim_stale("w3", idle_ms=0)
    assert _drive(queue, admission, provider, redis_client, *reclaimed) == (
        JobOutcome.AMBIGUOUS_RECOVERY
    )
    assert dograh.calls == 1  # STILL exactly one real call
    assert _pending(queue) == 0
    (attempt,) = _attempts(contact_id)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    with _Session() as s:
        event = s.execute(
            select(CallEvent).where(CallEvent.call_attempt_id == attempt.id)
        ).scalars().all()
        assert [e.event_type for e in event if "AMBIGUOUS" in e.event_type] == [
            "DOGRAH_TRIGGER_AMBIGUOUS"
        ]
        assert s.get(Contact, contact_id).status == ContactStatus.RETRY_SCHEDULED
    now = datetime.now(UTC)
    scheduler = get_recovery_scheduler()
    assert not [j for j in scheduler.due_jobs(now) if contact_id in j]  # backed off
    assert len([j for j in scheduler.due_jobs(now + timedelta(seconds=45)) if contact_id in j]) == 1


def test_duplicate_delivery_after_failed_trigger_is_not_retriggered(
    dograh, redis_client, provider
):
    """Pre-existing hole: an existing attempt WITHOUT a provider_call_id
    (every failed trigger) used to be re-dialed on a duplicate delivery."""
    dograh.error = DograhApiError(
        503, "unavailable", category=DograhErrorCategory.PROVIDER_UNAVAILABLE
    )
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "dup"), _admission(redis_client)
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1)
    queue.enqueue(job)
    queue.enqueue(job)  # duplicate delivery of the same logical job

    for _ in range(2):
        message_id, delivered = queue.read_one("w1", 100)
        outcome = _drive(queue, admission, provider, redis_client, message_id, delivered)

    assert outcome == JobOutcome.ALREADY_PROCESSED
    assert dograh.calls == 1 and len(_attempts(contact_id)) == 1


# -- 2B. Redis outage ---------------------------------------------------------


def test_redis_down_during_admission_fails_closed_then_recovers_without_duplicate(
    dograh, redis_client, provider
):
    campaign_id, (contact_id,) = _world()
    queue = _queue(redis_client, "rd1")
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)

    dead = AdmissionController(
        DeadRedis(),
        global_cps_limit=1, campaign_cps_limit=1, provider_cps_limit=1,
        global_concurrency_limit=1, campaign_concurrency_limit=1, provider_concurrency_limit=1,
    )
    with pytest.raises(redis_lib.exceptions.ConnectionError):
        _drive(queue, dead, provider, redis_client, message_id, job)

    # Fail closed: no admission => no call, no attempt, job not acked.
    assert dograh.calls == 0 and _attempts(contact_id) == [] and _pending(queue) == 1

    # Redis recovers; the worker resumes safely.
    (reclaimed,) = queue.reclaim_stale("w2", idle_ms=0)
    admission = _admission(redis_client)
    assert _drive(queue, admission, provider, redis_client, *reclaimed) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    assert dograh.calls == 1 and _pending(queue) == 0

    # No critical business state lives only in Redis: wipe Redis entirely and
    # re-deliver the same job -- PostgreSQL alone prevents a second dial.
    redis_client.flushdb()
    queue2 = _queue(redis_client, "rd1b")
    queue2.enqueue(job)
    message_id2, job2 = queue2.read_one("w3", 100)
    assert _drive(queue2, admission, provider, redis_client, message_id2, job2) == (
        JobOutcome.ALREADY_PROCESSED
    )
    assert dograh.calls == 1 and len(_attempts(contact_id)) == 1


def test_redis_down_during_queue_read_changes_nothing_and_resumes(
    dograh, redis_client, provider
):
    from app.services.queue.dialer_worker import process_one_job

    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "rd2"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))

    live = queue.redis
    queue.redis = DeadRedis()
    with _Session() as db, pytest.raises(redis_lib.exceptions.ConnectionError):
        process_one_job(
            db, queue, admission, provider, CircuitBreaker(live, "dograh"), consumer_name="w1",
            block_ms=10,
        )
    assert dograh.calls == 0 and _attempts(contact_id) == []

    queue.redis = live  # Redis is back
    with _Session() as db:
        outcome = process_one_job(
            db, queue, admission, provider, CircuitBreaker(live, "dograh"), consumer_name="w1",
            block_ms=100,
        )
    assert outcome == JobOutcome.ADMITTED_AND_DIALED and dograh.calls == 1


# -- 2C. Retry storm ----------------------------------------------------------


def _process_all(queue, admission, provider, redis_client, consumer="w1") -> list[str]:
    outcomes = []
    while (read := queue.read_one(consumer, 10)) is not None:
        outcomes.append(_drive(queue, admission, provider, redis_client, *read))
    return outcomes


def test_retry_storm_stays_bounded_backed_off_and_rechecks_everything(
    dograh, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("CIRCUIT_BREAKER_ERROR_THRESHOLD", "1000000")  # isolate retry policy
    get_settings.cache_clear()
    dograh.error = DograhApiError(
        503, "unavailable", category=DograhErrorCategory.PROVIDER_UNAVAILABLE
    )
    n = 30
    campaign_id, contact_ids = _world(n)
    queue, admission = _queue(redis_client, "storm"), _admission(redis_client)
    scheduler = get_recovery_scheduler()
    base = datetime.now(UTC)

    def enqueue_round_one():
        for cid in contact_ids:
            queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))

    # Round 1: everyone fails at once.
    enqueue_round_one()
    _process_all(queue, admission, provider, redis_client)
    assert dograh.calls == n
    scheduled = redis_client.zrange("recovery:scheduled", 0, -1, withscores=True)
    assert len(scheduled) == n  # one retry each -- RecoveryManager is the only owner
    assert all(29 <= score - base.timestamp() <= 40 for _, score in scheduled)  # 30s backoff
    assert dispatch_due_recovery_jobs(scheduler, queue, now=base + timedelta(seconds=5)) == 0
    assert queue.redis.xlen(queue.stream_key) == n  # nothing re-enqueued immediately

    # Between rounds: opt-outs and a closed campaign are re-checked at dispatch.
    with _Session() as s:
        for cid in contact_ids[:3]:
            c = s.get(Contact, cid)
            s.add(
                Suppression(
                    contact_id=c.id,
                    phone_number=c.normalized_phone_number,
                    reason="opt-out",
                    source=SuppressionSource.MANUAL_API,
                )
            )
        s.commit()
    dispatched = dispatch_due_recovery_jobs(scheduler, queue, now=base + timedelta(seconds=45))
    assert dispatched == n - 3  # suppressed contacts are never retried

    # Round 2 (retry #1): all fail again -> next backoff is 10 minutes.
    _process_all(queue, admission, provider, redis_client)
    assert dograh.calls == n + (n - 3)
    scheduled = redis_client.zrange("recovery:scheduled", 0, -1, withscores=True)
    assert len(scheduled) == n - 3
    assert all(595 <= score - base.timestamp() <= 640 for _, score in scheduled)
    assert dispatch_due_recovery_jobs(scheduler, queue, now=base + timedelta(seconds=300)) == 0

    # Round 3 (retry #2): after the final failure nothing is scheduled again.
    assert dispatch_due_recovery_jobs(scheduler, queue, now=base + timedelta(seconds=700)) == n - 3
    _process_all(queue, admission, provider, redis_client)
    assert redis_client.zcard("recovery:scheduled") == 0  # max_retries=2 => stop
    assert dograh.calls == n + 2 * (n - 3)

    with _Session() as s:
        per_contact = dict(
            s.execute(
                select(CallAttempt.contact_id, func.count())
                .where(CallAttempt.contact_id.in_(contact_ids))
                .group_by(CallAttempt.contact_id)
            ).all()
        )
    assert max(per_contact.values()) == 3  # 1 initial + 2 retries, never more
    assert sorted(per_contact.values()).count(1) == 3  # only the suppressed three stopped at 1
    total = sum(per_contact.values())
    assert total == dograh.calls  # exactly one attempt row per real trigger: no duplicates


def test_open_circuit_stops_new_traffic_then_resumes(dograh, redis_client, provider):
    dograh.error = DograhApiError(
        503, "unavailable", category=DograhErrorCategory.PROVIDER_UNAVAILABLE
    )
    campaign_id, contact_ids = _world(12)
    queue, admission = _queue(redis_client, "brk"), _admission(redis_client)
    for cid in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))

    outcomes = _process_all(queue, admission, provider, redis_client)
    threshold = get_settings().circuit_breaker_error_threshold
    assert outcomes.count(JobOutcome.ADMITTED_AND_DIALED) == threshold
    assert outcomes.count(JobOutcome.NOT_ADMITTED) == 12 - threshold
    assert dograh.calls == threshold  # the open circuit stopped every further call
    assert _pending(queue) == 12 - threshold  # held back, not dropped

    redis_client.delete("circuit:dograh:open")  # cool-down elapsed
    dograh.error = None
    for message_id, job in queue.reclaim_stale("w2", idle_ms=0, count=50):
        _drive(queue, admission, provider, redis_client, message_id, job)
    assert _pending(queue) == 0 and dograh.calls == 12
    assert all(len(_attempts(cid)) == 1 for cid in contact_ids)


# -- 2D. Graceful shutdown ------------------------------------------------------


@pytest.fixture
def worker_env(dograh, redis_client):
    """Runs the REAL app.worker.run() in the main thread with real signals."""
    import app.worker as worker

    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    worker._shutdown_requested = False
    yield worker
    for sig, handler in old.items():
        signal.signal(sig, handler)
    worker._shutdown_requested = False


def _run_worker_until_signalled(worker, kill_after: float) -> float:
    timer = threading.Timer(kill_after, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        worker.run()
    finally:
        timer.cancel()
    worker._shutdown_requested = False
    return time.monotonic() - started


def test_shutdown_while_idle_exits_promptly(worker_env):
    elapsed = _run_worker_until_signalled(worker_env, 0.3)
    assert elapsed < 5


@pytest.mark.parametrize("point", ["before_trigger", "after_trigger_before_ack"])
def test_shutdown_mid_job_finishes_it_durably_stops_claiming_and_restart_continues(
    point, worker_env, dograh, redis_client, monkeypatch
):
    from app.services.queue.factory import get_queue

    campaign_id, (c1, c2) = _world(2)
    queue = get_queue()
    for cid in (c1, c2):
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))

    fired = {"done": False}

    def send_sigterm_once():
        if not fired["done"]:
            fired["done"] = True
            os.kill(os.getpid(), signal.SIGTERM)

    if point == "before_trigger":
        real = CallAttemptRepository.get_or_create

        def hooked(self, *a, **k):
            send_sigterm_once()  # worker holds the job, provider not yet called
            return real(self, *a, **k)

        monkeypatch.setattr(CallAttemptRepository, "get_or_create", hooked)
    else:
        real_ack = RedisStreamQueue.ack

        def hooked_ack(self, message_id):
            send_sigterm_once()  # trigger done + committed, ACK not yet sent
            return real_ack(self, message_id)

        monkeypatch.setattr(RedisStreamQueue, "ack", hooked_ack)

    _run_worker_until_signalled(worker_env, kill_after=30)  # signal comes from the hook

    # The in-flight job finished durably and was acked; the second was never claimed.
    assert dograh.calls == 1
    assert _pending(queue) == 0
    done = [cid for cid in (c1, c2) if _attempts(cid)]
    assert len(done) == 1 and _attempts(done[0])[0].provider_call_id == str(dograh.run_ids[0])

    # Restart: the remaining job is processed; the first is NOT dialed again.
    _run_worker_until_signalled(worker_env, kill_after=1.5)
    assert dograh.calls == 2
    assert all(len(_attempts(cid)) == 1 for cid in (c1, c2))
    assert _pending(queue) == 0


# -- Phase 1: reconciliation of an ambiguous trigger ---------------------------

AMBIGUOUS = DograhApiError(
    0, "timed out waiting for response", category=DograhErrorCategory.AMBIGUOUS_REQUEST
)


def _events(attempt_id) -> dict[str, dict]:
    with _Session() as s:
        rows = s.execute(select(CallEvent).where(CallEvent.call_attempt_id == attempt_id)).scalars()
        return {e.event_type: e.payload for e in rows}


def _run_one(dograh, redis_client, provider, name):
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, name), _admission(redis_client)
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1)
    queue.enqueue(job)
    message_id, delivered = queue.read_one("w1", 100)
    outcome = _drive(queue, admission, provider, redis_client, message_id, delivered)
    return queue, admission, job, contact_id, outcome


def _scheduled_for(contact_id: str, seconds_ahead: int) -> int:
    now = datetime.now(UTC) + timedelta(seconds=seconds_ahead)
    return len([j for j in get_recovery_scheduler().due_jobs(now) if contact_id in j])


def test_ambiguous_trigger_with_one_existing_run_is_adopted_not_redialed(
    dograh, redis_client, provider
):
    dograh.error = AMBIGUOUS
    adopted_run = next(_RUN_IDS)
    dograh.runs = [adopted_run]

    queue, admission, job, contact_id, outcome = _run_one(dograh, redis_client, provider, "rc1")

    assert outcome == JobOutcome.RECONCILED
    assert dograh.calls == 1 and dograh.reconcile_calls == 1 and _pending(queue) == 0
    (attempt,) = _attempts(contact_id)
    assert attempt.provider_call_id == str(adopted_run)
    assert attempt.state == CallAttemptState.INITIATED  # never CONNECTED: the webhook decides
    assert _events(attempt.id)["DOGRAH_TRIGGER_RECONCILED"] == {
        "workflow_run_id": adopted_run,
        "stage": "first",
    }
    assert _scheduled_for(contact_id, 3600) == 0  # no retry: the call already exists
    with _Session() as s:
        assert s.get(Contact, contact_id).status == ContactStatus.DIALING

    # Repeated delivery / repeated reconciliation is an idempotent no-op.
    queue.enqueue(job)
    message_id, delivered = queue.read_one("w1", 100)
    assert _drive(queue, admission, provider, redis_client, message_id, delivered) == (
        JobOutcome.ALREADY_PROCESSED
    )
    assert dograh.calls == 1 and dograh.reconcile_calls == 1 and len(_attempts(contact_id)) == 1


def test_ambiguous_trigger_with_no_run_found_falls_back_to_backoff(
    dograh, redis_client, provider
):
    dograh.error = AMBIGUOUS
    dograh.runs = []

    _, _, _, contact_id, outcome = _run_one(dograh, redis_client, provider, "rc2")

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    (attempt,) = _attempts(contact_id)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert _events(attempt.id)["DOGRAH_TRIGGER_AMBIGUOUS"]["reconcile"] == "no_run_found"
    # "None found" is not proof of absence: the normal 30s backoff still applies.
    assert _scheduled_for(contact_id, 5) == 0 and _scheduled_for(contact_id, 45) == 1


def test_ambiguous_trigger_with_several_runs_never_picks_one_and_never_retries(
    dograh, redis_client, provider
):
    dograh.error = AMBIGUOUS
    dograh.runs = [next(_RUN_IDS), next(_RUN_IDS)]

    queue, _, _, contact_id, outcome = _run_one(dograh, redis_client, provider, "rc3")

    assert outcome == JobOutcome.RECONCILE_MULTIPLE
    (attempt,) = _attempts(contact_id)
    assert attempt.provider_call_id is None  # nothing silently chosen
    assert attempt.state == CallAttemptState.INITIATED  # webhooks resolve it
    assert _scheduled_for(contact_id, 3600) == 0  # a third call would only make it worse
    assert "DOGRAH_RECONCILE_MULTIPLE" in _events(attempt.id)
    with _Session() as s:
        audit = s.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_id = :i AND action = :a"),
            {"i": attempt.id, "a": "dograh.reconciliation_multiple_runs"},
        ).scalar_one()
    assert audit == 1 and dograh.calls == 1 and _pending(queue) == 0


@pytest.mark.parametrize(
    "error",
    [
        DograhApiError(503, "down", category=DograhErrorCategory.PROVIDER_UNAVAILABLE),
        DograhApiError(0, "timed out", category=DograhErrorCategory.AMBIGUOUS_REQUEST),
        DograhApiError(0, "refused", category=DograhErrorCategory.CONNECTION_ERROR),
    ],
    ids=["provider_unavailable", "reconcile_timeout", "connection_error"],
)
def test_reconciliation_unavailable_falls_back_to_backoff_without_retrying_inline(
    error, dograh, redis_client, provider
):
    dograh.error = AMBIGUOUS
    dograh.reconcile_error = error

    _, _, _, contact_id, outcome = _run_one(dograh, redis_client, provider, "rc4")

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert dograh.reconcile_calls == 1  # one bounded attempt, no inline retry loop
    (attempt,) = _attempts(contact_id)
    assert _events(attempt.id)["DOGRAH_TRIGGER_AMBIGUOUS"]["reconcile"] == "unavailable"
    assert _scheduled_for(contact_id, 5) == 0 and _scheduled_for(contact_id, 45) == 1


def test_non_ambiguous_failure_does_not_reconcile(dograh, redis_client, provider):
    dograh.error = DograhApiError(400, "bad", category=DograhErrorCategory.VALIDATION_ERROR)

    _run_one(dograh, redis_client, provider, "rc5")

    assert dograh.reconcile_calls == 0  # a definite rejection created no run


def test_orphaned_claim_adopts_the_existing_run_instead_of_retrying(
    dograh, redis_client, provider, monkeypatch
):
    """The lost-commit scenario, now with a verifiable answer: the call that was
    placed is found and adopted, so not even a backoff retry is scheduled."""
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "rc6"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)

    with _Session() as db:
        real_commit, n = db.commit, {"c": 0}

        def flaky_commit():
            n["c"] += 1
            if n["c"] == 2:
                raise _transient_db_error()
            real_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        with pytest.raises(OperationalError):
            process_claimed_job(
                db, queue, admission, provider, CircuitBreaker(redis_client, "dograh"),
                message_id, job,
            )
    with _Session() as s:
        s.execute(text("UPDATE call_attempt SET started_at = now() - interval '1 hour'"))
        s.commit()

    placed_run = next(_RUN_IDS)
    dograh.runs = [placed_run]
    (reclaimed,) = queue.reclaim_stale("w2", idle_ms=0)
    assert _drive(queue, admission, provider, redis_client, *reclaimed) == JobOutcome.RECONCILED

    assert dograh.calls == 1 and _pending(queue) == 0
    (attempt,) = _attempts(contact_id)
    assert attempt.provider_call_id == str(placed_run)
    assert _scheduled_for(contact_id, 3600) == 0
