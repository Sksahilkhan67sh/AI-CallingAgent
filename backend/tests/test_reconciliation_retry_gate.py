"""CP09 follow-up -- second reconciliation immediately before an ambiguous
Dograh retry is dialed.

RecoveryManager still decides *whether* to retry; the gate only decides
whether that already-approved retry is still safe. It narrows the duplicate-call
window and does NOT make it impossible (see docs/CHECKPOINT-09-NOTES.md §19).
Verified against the published Dograh run listing only, not a live instance.
"""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    SuppressionSource,
)
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job
from app.services.queue.job import DialJob
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhConfigurationError,
    DograhErrorCategory,
)
from tests.test_failure_injection import (
    _RUN_IDS,
    AMBIGUOUS,
    DeadRedis,
    FakeDograh,
    _admission,
    _attempts,
    _drive,
    _events,
    _pending,
    _queue,
    _Session,
    _transient_db_error,
    _world,
)

# The `fake` fixture patches time.sleep globally (to skip admission back-off);
# the lookup delay below must still be a REAL delay or the race is never exercised.
_REAL_SLEEP = time.sleep


class GateFake(FakeDograh):
    """FakeDograh plus the knobs these tests need around the lookups."""

    def __init__(self) -> None:
        super().__init__()
        self.lookup_delay_s = 0.0
        self.crash_on_lookup = False
        self.crash_before_trigger = False
        self.lookup_attempts: list[str] = []

    def find_runs_for_attempt(self, call_attempt_id, since):
        self.lookup_attempts.append(call_attempt_id)
        if self.crash_on_lookup:
            raise RuntimeError("worker crashed during lookup")
        _REAL_SLEEP(self.lookup_delay_s)
        return super().find_runs_for_attempt(call_attempt_id, since)

    def trigger_call(self, *, phone_number, initial_context):
        if self.crash_before_trigger:
            raise RuntimeError("worker crashed immediately before the trigger")
        return super().trigger_call(phone_number=phone_number, initial_context=initial_context)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    f = GateFake()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: f)
    monkeypatch.setattr("app.services.queue.dialer_worker.time.sleep", lambda _s: None)
    yield f
    get_settings.cache_clear()


@dataclass
class Scenario:
    queue: object
    admission: object
    campaign_id: str
    contact_id: str


def _first_attempt_ambiguous(fake, redis_client, provider, name, **world_kw) -> Scenario:
    """Attempt 1 times out ambiguously, the first lookup finds nothing, and
    RecoveryManager schedules the 30s retry."""
    campaign_id, (contact_id,) = _world(**world_kw)
    queue, admission = _queue(redis_client, name), _admission(redis_client)
    fake.error, fake.runs = AMBIGUOUS, []
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)
    assert _drive(queue, admission, provider, redis_client, message_id, job) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    fake.error = None
    assert (fake.calls, fake.reconcile_calls) == (1, 1)
    return Scenario(queue, admission, campaign_id, contact_id)


def _due_retry(sc: Scenario):
    """The retry becomes due: RecoveryManager's dispatcher enqueues it."""
    dispatched = dispatch_due_recovery_jobs(
        get_recovery_scheduler(), sc.queue, now=datetime.now(UTC) + timedelta(seconds=45)
    )
    assert dispatched == 1
    return sc.queue.read_one("w2", 100)


def _process(sc, redis_client, provider, read):
    return _drive(sc.queue, sc.admission, provider, redis_client, *read)


def _audit_count(attempt_id, action) -> int:
    with _Session() as s:
        return len(
            s.execute(
                select(AuditLog.id).where(
                    AuditLog.entity_id == attempt_id, AuditLog.action == action
                )
            ).all()
        )


def _contact_status(contact_id):
    with _Session() as s:
        return s.get(Contact, contact_id).status


# -- TESTS 1-6: the gate's outcomes ------------------------------------------------


def test_1_second_lookup_finds_one_run_adopts_it_and_never_dials(
    fake, redis_client, provider, client
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g1")
    run = next(_RUN_IDS)
    fake.runs = [run]

    outcome = _process(sc, redis_client, provider, _due_retry(sc))

    assert outcome == JobOutcome.RECONCILED
    assert fake.calls == 1  # no new Dograh trigger
    (attempt,) = _attempts(sc.contact_id)  # attempt 2 was never even created
    assert attempt.provider_call_id == str(run)
    assert attempt.state == CallAttemptState.INITIATED  # not CONNECTED
    assert _contact_status(sc.contact_id) == ContactStatus.DIALING
    assert _events(attempt.id)["DOGRAH_TRIGGER_RECONCILED"] == {
        "workflow_run_id": run,
        "stage": "second",
    }
    assert _audit_count(attempt.id, "dograh.reconciliation_run_adopted") == 1
    assert _pending(sc.queue) == 0
    # The point of reopening the attempt: the real call's completion webhook
    # is processed, not dropped as "already_processed".
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "workflow_run_id": run,
            "call_status": "user_hangup",
        },
        headers={"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"},
    )
    assert response.json()["outcome"] == "ended_normally"


def test_2_second_lookup_finds_nothing_exactly_one_retry_trigger(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g2")

    outcome = _process(sc, redis_client, provider, _due_retry(sc))

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2  # the original + exactly one retry
    assert fake.reconcile_calls == 2  # first lookup + ONE second lookup, no third
    first, second = _attempts(sc.contact_id)
    assert (second.attempt_number, second.provider_call_id) == (2, str(fake.run_ids[-1]))
    assert _audit_count(first.id, "dograh.reconciliation_retry_allowed") == 1


def test_3_second_lookup_finds_several_runs_never_dials_never_chooses(
    fake, redis_client, provider
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g3")
    fake.runs = [next(_RUN_IDS), next(_RUN_IDS)]

    outcome = _process(sc, redis_client, provider, _due_retry(sc))

    assert outcome == JobOutcome.RECONCILE_MULTIPLE
    assert fake.calls == 1
    (attempt,) = _attempts(sc.contact_id)
    assert attempt.provider_call_id is None  # nothing silently chosen
    assert attempt.state == CallAttemptState.INITIATED  # webhooks resolve it
    assert _audit_count(attempt.id, "dograh.reconciliation_multiple_runs") == 1
    assert _pending(sc.queue) == 0


@pytest.mark.parametrize(
    "error,kind",
    [
        (DograhApiError(0, "timed out", category=DograhErrorCategory.AMBIGUOUS_REQUEST), "timeout"),
        (DograhApiError(0, "connect timed out", category=DograhErrorCategory.TIMEOUT), "timeout"),
        (DograhApiError(503, "down", category=DograhErrorCategory.PROVIDER_UNAVAILABLE), "error"),
        (DograhApiError(401, "nope", category=DograhErrorCategory.AUTHENTICATION_ERROR), "error"),
        (DograhConfigurationError("no api key"), "error"),
    ],
    ids=["read_timeout", "connect_timeout", "provider_error", "auth_error", "misconfigured"],
)
def test_4_5_second_lookup_failure_fails_closed_and_recovers_without_duplicate(
    error, kind, fake, redis_client, provider
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g4")
    read = _due_retry(sc)
    fake.reconcile_error = error

    outcome = _process(sc, redis_client, provider, read)

    assert outcome == JobOutcome.RECONCILE_DEFERRED
    assert fake.calls == 1  # safety could not be established -> no call
    assert len(_attempts(sc.contact_id)) == 1
    assert _pending(sc.queue) == 1  # held, not dropped
    (first,) = _attempts(sc.contact_id)
    assert _audit_count(first.id, f"dograh.reconciliation_lookup_{kind}") == 1

    # Dograh recovers: the redelivered retry is re-checked, then dialed once.
    fake.reconcile_error = None
    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2 and _pending(sc.queue) == 0


def test_6_repeated_reconciliation_is_idempotent(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g6")
    run = next(_RUN_IDS)
    fake.runs = [run]
    read = _due_retry(sc)
    assert _process(sc, redis_client, provider, read) == JobOutcome.RECONCILED
    lookups = fake.reconcile_calls

    # The same retry job is delivered again, repeatedly.
    for _ in range(3):
        sc.queue.enqueue(read[1])
        again = sc.queue.read_one("w3", 100)
        assert _process(sc, redis_client, provider, again) == JobOutcome.ALREADY_PROCESSED

    assert fake.calls == 1 and fake.reconcile_calls == lookups  # no new lookup, no dial
    (attempt,) = _attempts(sc.contact_id)
    assert attempt.provider_call_id == str(run)
    assert _audit_count(attempt.id, "dograh.reconciliation_run_adopted") == 1


# -- TESTS 7-8: concurrency -----------------------------------------------------------


def _race(sc, redis_client, provider, n, read):
    """n workers each hold a delivery of the SAME retry job and run together."""
    for _ in range(n - 1):
        sc.queue.enqueue(read[1])
    reads = [read] + [sc.queue.read_one(f"r{i}", 100) for i in range(n - 1)]
    barrier = threading.Barrier(n)
    outcomes: list = [None] * n

    def worker(i):
        barrier.wait()
        try:
            outcomes[i] = _process(sc, redis_client, provider, reads[i])
        except Exception as exc:  # surfaced by the assertions below
            outcomes[i] = f"ERR:{type(exc).__name__}:{exc}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


@pytest.mark.parametrize("n", [2, 8])
def test_7_8_concurrent_workers_on_one_retry_trigger_exactly_once(
    n, fake, redis_client, provider
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, f"g7_{n}")
    read = _due_retry(sc)
    fake.lookup_delay_s = 0.2  # widen the race: every worker is inside its lookup together

    outcomes = _race(sc, redis_client, provider, n, read)

    assert not [o for o in outcomes if str(o).startswith("ERR")], outcomes
    assert fake.calls == 2  # the original + EXACTLY ONE retry, however many workers
    assert [a.attempt_number for a in _attempts(sc.contact_id)] == [1, 2]
    assert outcomes.count(JobOutcome.ADMITTED_AND_DIALED) == 1

    # CP12-A: one logical attempt holds ONE concurrency lease, so a duplicate delivery that
    # arrives while the winner is in flight is not admitted. It is left unacked (never lost
    # and never a second dial) and the normal stale-reclaim pass resolves it without any
    # further trigger. Before CP12-A every duplicate took its own slot and was acked at once.
    assert _pending(sc.queue) == outcomes.count(JobOutcome.NOT_ADMITTED)
    for reclaimed in sc.queue.reclaim_stale("drain", idle_ms=0):
        assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ALREADY_PROCESSED
    assert _pending(sc.queue) == 0
    assert fake.calls == 2


@pytest.mark.parametrize("n", [2, 8])
def test_7_8_concurrent_workers_adopting_one_run_dial_nothing_and_adopt_once(
    n, fake, redis_client, provider
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, f"g8_{n}")
    run = next(_RUN_IDS)
    fake.runs = [run]
    read = _due_retry(sc)
    fake.lookup_delay_s = 0.2

    outcomes = _race(sc, redis_client, provider, n, read)

    assert not [o for o in outcomes if str(o).startswith("ERR")], outcomes
    assert fake.calls == 1  # nobody dialed
    (attempt,) = _attempts(sc.contact_id)
    assert attempt.provider_call_id == str(run)
    assert outcomes.count(JobOutcome.RECONCILED) == 1  # adopted once, not n times
    assert _audit_count(attempt.id, "dograh.reconciliation_run_adopted") == 1


# -- TEST 9, 10, 11: scope and bounds -------------------------------------------------


def test_9_non_ambiguous_failure_never_triggers_any_lookup(fake, redis_client, provider):
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "g9"), _admission(redis_client)
    fake.error = DograhApiError(400, "bad", category=DograhErrorCategory.VALIDATION_ERROR)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    assert _drive(queue, admission, provider, redis_client, *queue.read_one("w1", 100))
    sc = Scenario(queue, admission, campaign_id, contact_id)
    fake.error = None

    outcome = _process(sc, redis_client, provider, _due_retry(sc))

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2  # the retry went ahead...
    assert fake.reconcile_calls == 0  # ...with no lookup before or after


def test_10_calls_that_never_failed_ambiguously_do_no_lookup(fake, redis_client, provider):
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "g10"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    assert _drive(queue, admission, provider, redis_client, *queue.read_one("w1", 100)) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    assert fake.calls == 1 and fake.reconcile_calls == 0


def test_11_retry_budget_unchanged_three_attempts_maximum(fake, redis_client, provider):
    """Ambiguous every time, nothing ever found: 1 initial + retry #1 (30s) +
    retry #2 (10min) and then stop -- the gate never adds or removes an attempt."""
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g11")
    fake.error = AMBIGUOUS  # every further trigger is ambiguous too
    base = datetime.now(UTC)
    scheduler = get_recovery_scheduler()

    scores = [s for _, s in redis_client.zrange("recovery:scheduled", 0, -1, withscores=True)]
    assert 29 <= scores[0] - base.timestamp() <= 40  # retry #1 = 30s
    assert dispatch_due_recovery_jobs(scheduler, sc.queue, now=base + timedelta(seconds=45)) == 1
    _process(sc, redis_client, provider, sc.queue.read_one("w2", 100))

    scores = [s for _, s in redis_client.zrange("recovery:scheduled", 0, -1, withscores=True)]
    assert 595 <= scores[0] - base.timestamp() <= 640  # retry #2 = 10 min
    assert dispatch_due_recovery_jobs(scheduler, sc.queue, now=base + timedelta(seconds=700)) == 1
    _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))

    assert redis_client.zcard("recovery:scheduled") == 0  # max_retries=2 => stop
    assert [a.attempt_number for a in _attempts(sc.contact_id)] == [1, 2, 3]
    assert fake.calls == 3
    # lookups: a first one per ambiguous trigger (3) + a second one before each
    # of the two retries (2) -- and nothing else.
    assert fake.reconcile_calls == 5


# -- TESTS 12-14: the gate never bypasses eligibility ------------------------------------


def test_12_suppressed_contact_is_never_looked_up_or_dialed(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g12")
    retry_job = DialJob.new(
        campaign_id=sc.campaign_id, contact_id=sc.contact_id, attempt_number=2
    )
    with _Session() as s:
        c = s.get(Contact, sc.contact_id)
        s.add(
            Suppression(
                contact_id=c.id,
                phone_number=c.normalized_phone_number,
                reason="opt-out",
                source=SuppressionSource.MANUAL_API,
            )
        )
        s.commit()
    assert dispatch_due_recovery_jobs(
        get_recovery_scheduler(), sc.queue, now=datetime.now(UTC) + timedelta(seconds=45)
    ) == 0  # the dispatcher re-checks suppression

    sc.queue.enqueue(retry_job)  # ...and even a job that reaches a worker is refused
    outcome = _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))

    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert fake.calls == 1 and fake.reconcile_calls == 1  # no second lookup, no dial


def test_13_paused_campaign_no_lookup_no_trigger(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g13")
    with _Session() as s:
        s.get(Campaign, sc.campaign_id).status = CampaignStatus.PAUSED
        s.commit()
    assert dispatch_due_recovery_jobs(
        get_recovery_scheduler(), sc.queue, now=datetime.now(UTC) + timedelta(seconds=45)
    ) == 0

    sc.queue.enqueue(
        DialJob.new(campaign_id=sc.campaign_id, contact_id=sc.contact_id, attempt_number=2)
    )
    outcome = _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))

    # CP12-B: the retry job is HELD (left unacked), not acked-and-dropped as NOT_ELIGIBLE --
    # a paused retry must survive until the campaign resumes.
    assert outcome == JobOutcome.CAMPAIGN_PAUSED_HELD
    assert _pending(sc.queue) == 1
    assert fake.calls == 1 and fake.reconcile_calls == 1


def test_14_calling_window_closed_no_lookup_no_trigger(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "g14")
    hour = (datetime.now(UTC).hour + 12) % 24
    with _Session() as s:
        policy = s.execute(
            select(RetryPolicy).where(RetryPolicy.campaign_id == sc.campaign_id)
        ).scalar_one()
        policy.window_start, policy.window_end = dtime(hour, 0), dtime(hour, 1)
        s.commit()

    sc.queue.enqueue(
        DialJob.new(campaign_id=sc.campaign_id, contact_id=sc.contact_id, attempt_number=2)
    )
    outcome = _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))

    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert fake.calls == 1 and fake.reconcile_calls == 1


# -- SECTION 9: failure injection -----------------------------------------------------------
# Invariant for every case: never intentionally create a duplicate Dograh call.


def test_fi_first_lookup_failure_then_second_lookup_decides(fake, redis_client, provider):
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "fi1"), _admission(redis_client)
    fake.error = AMBIGUOUS
    fake.reconcile_error = DograhApiError(
        503, "down", category=DograhErrorCategory.PROVIDER_UNAVAILABLE
    )
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    _drive(queue, admission, provider, redis_client, *queue.read_one("w1", 100))
    fake.error = fake.reconcile_error = None  # Dograh is back; the run DID exist
    fake.runs = [next(_RUN_IDS)]
    sc = Scenario(queue, admission, campaign_id, contact_id)

    assert _process(sc, redis_client, provider, _due_retry(sc)) == JobOutcome.RECONCILED
    assert fake.calls == 1  # the second lookup caught what the first could not


def test_fi_db_commit_failure_while_adopting_loses_nothing_and_never_dials(
    fake, redis_client, provider, monkeypatch
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi2")
    run = next(_RUN_IDS)
    fake.runs = [run]
    message_id, job = _due_retry(sc)

    with _Session() as db:
        real_commit = db.commit

        def failing_commit():
            monkeypatch.setattr(db, "commit", real_commit)  # fail once only
            raise _transient_db_error()

        monkeypatch.setattr(db, "commit", failing_commit)
        with pytest.raises(OperationalError):
            process_claimed_job(
                db, sc.queue, sc.admission, provider, CircuitBreaker(redis_client, "dograh"),
                message_id, job,
            )

    (attempt,) = _attempts(sc.contact_id)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT  # rolled back whole
    assert attempt.provider_call_id is None and _pending(sc.queue) == 1

    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.RECONCILED
    assert fake.calls == 1
    assert _audit_count(attempt.id, "dograh.reconciliation_run_adopted") == 1


def test_fi_db_read_failure_before_the_gate_changes_nothing(
    fake, redis_client, provider, monkeypatch
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi3")
    read = _due_retry(sc)
    real, n = CallAttemptRepository.get_by_contact_and_number, {"n": 0}

    def flaky(self, *a, **k):
        n["n"] += 1
        if n["n"] == 2:  # 1st = idempotency check; 2nd = the gate's previous-attempt read
            raise _transient_db_error()
        return real(self, *a, **k)

    monkeypatch.setattr(CallAttemptRepository, "get_by_contact_and_number", flaky)
    with pytest.raises(OperationalError):
        _process(sc, redis_client, provider, read)
    assert fake.calls == 1 and fake.reconcile_calls == 1 and _pending(sc.queue) == 1

    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2


def test_fi_redis_down_during_admission_reaches_neither_lookup_nor_trigger(
    fake, redis_client, provider
):
    from app.services.queue.admission_controller import AdmissionController

    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi4")
    read = _due_retry(sc)
    dead = AdmissionController(
        DeadRedis(),
        global_cps_limit=1, campaign_cps_limit=1, provider_cps_limit=1,
        global_concurrency_limit=1, campaign_concurrency_limit=1, provider_concurrency_limit=1,
    )
    with pytest.raises(Exception, match="redis is down"):
        _drive(sc.queue, dead, provider, redis_client, *read)
    assert fake.calls == 1 and fake.reconcile_calls == 1  # fail closed: nothing new happened

    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)  # Redis back
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2


def test_fi_worker_crash_before_second_lookup(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi5")
    read = _due_retry(sc)
    fake.crash_on_lookup = True
    with pytest.raises(RuntimeError):
        _process(sc, redis_client, provider, read)
    assert fake.calls == 1 and _pending(sc.queue) == 1 and len(_attempts(sc.contact_id)) == 1

    fake.crash_on_lookup = False  # restart
    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2  # one retry, not two


def test_fi_worker_crash_after_second_lookup_before_claim(
    fake, redis_client, provider, monkeypatch
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi6")
    read = _due_retry(sc)
    real, n = CallAttemptRepository.get_or_create, {"n": 0}

    def crash_once(self, *a, **k):
        n["n"] += 1
        if n["n"] == 1:
            raise RuntimeError("worker crashed after the lookup")
        return real(self, *a, **k)

    monkeypatch.setattr(CallAttemptRepository, "get_or_create", crash_once)
    with pytest.raises(RuntimeError):
        _process(sc, redis_client, provider, read)
    assert fake.calls == 1 and fake.reconcile_calls == 2  # the lookup ran, nothing was dialed

    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)  # restart: looked up AGAIN, then dials
    assert _process(sc, redis_client, provider, reclaimed) == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2 and fake.reconcile_calls == 3


def test_fi_worker_crash_immediately_before_retry_trigger_never_duplicates(
    fake, redis_client, provider
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi7")
    read = _due_retry(sc)
    fake.crash_before_trigger = True
    with pytest.raises(RuntimeError):
        _process(sc, redis_client, provider, read)
    # the durable claim for attempt 2 exists; the trigger never left
    assert [a.attempt_number for a in _attempts(sc.contact_id)] == [1, 2]
    assert fake.calls == 1 and _pending(sc.queue) == 1

    fake.crash_before_trigger = False
    with _Session() as s:
        s.execute(text("UPDATE call_attempt SET started_at = now() - interval '1 hour'"))
        s.commit()
    (reclaimed,) = sc.queue.reclaim_stale("w3", idle_ms=0)
    outcome = _process(sc, redis_client, provider, reclaimed)

    # An unresolved claim is never blindly re-triggered: it goes back through
    # RecoveryManager (and a lookup) -- worst case one fewer call, never two.
    assert outcome == JobOutcome.AMBIGUOUS_RECOVERY
    assert fake.calls == 1


def test_fi_redelivered_retry_job_after_a_completed_retry_is_a_noop(fake, redis_client, provider):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "fi8")
    read = _due_retry(sc)
    assert _process(sc, redis_client, provider, read) == JobOutcome.ADMITTED_AND_DIALED
    lookups = fake.reconcile_calls

    sc.queue.enqueue(read[1])
    outcome = _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))

    assert outcome == JobOutcome.ALREADY_PROCESSED
    assert fake.calls == 2 and fake.reconcile_calls == lookups


# -- adoption -> completion webhook -> terminal state -> analysis ------------------------

WEBHOOK_URL = "/api/v1/webhooks/dograh/call-completed"


def _auth():
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


def _post_webhook(web, attempt, run_id, status, *, headers=None):
    return web.post(
        WEBHOOK_URL,
        json={"call_attempt_id": str(attempt.id), "workflow_run_id": run_id, "call_status": status},
        headers=_auth() if headers is None else headers,
    )


@pytest.fixture
def real_client():
    """The real app with real get_db commits (no dependency override), so the
    worker's separate sessions see exactly what the webhook committed."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _sessions_and_analysis(attempt_id) -> tuple[int, int]:
    from app.models.conversation import ConversationSession

    with _Session() as s:
        sessions = len(
            s.execute(
                select(ConversationSession.id).where(
                    ConversationSession.call_attempt_id == attempt_id
                )
            ).all()
        )
    return sessions, _audit_count(attempt_id, "analysis.queued")


def test_adopted_attempt_completes_via_webhook_exactly_once_and_is_analysed(
    fake, redis_client, provider, real_client
):
    """FAILED_TO_CONNECT -> (one run found) -> INITIATED/DIALING -> completion
    webhook -> terminal -> post-call analysis."""
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "w3")
    run = next(_RUN_IDS)
    fake.runs = [run]
    assert _process(sc, redis_client, provider, _due_retry(sc)) == JobOutcome.RECONCILED
    (attempt,) = _attempts(sc.contact_id)
    assert attempt.state == CallAttemptState.INITIATED  # reopened, never CONNECTED

    # Webhook validation is NOT bypassed by adoption.
    unauthenticated = _post_webhook(real_client, attempt, run, "user_hangup", headers={})
    assert unauthenticated.status_code in (401, 403)
    assert _attempts(sc.contact_id)[0].state == CallAttemptState.INITIATED

    completed = _post_webhook(real_client, attempt, run, "user_hangup")
    assert completed.json()["outcome"] == "ended_normally"
    done = _attempts(sc.contact_id)[0]
    assert done.state == CallAttemptState.ENDED_NORMALLY  # terminal, and not CONNECTED
    assert _sessions_and_analysis(attempt.id) == (1, 1)

    # Replay: still exactly one session and one analysis job, and no extra dial.
    assert _post_webhook(real_client, attempt, run, "user_hangup").json()["outcome"] == (
        "already_processed"
    )
    assert _sessions_and_analysis(attempt.id) == (1, 1)
    assert fake.calls == 1 and len(_attempts(sc.contact_id)) == 1


def test_completion_webhook_arriving_before_the_second_lookup_is_not_lost(
    fake, redis_client, provider, real_client
):
    """The webhook beats the gate: the attempt is still FAILED_TO_CONNECT with
    no run id. It used to be dropped as already_processed (and a later
    adoption would then wait forever for a webhook that had already come)."""
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "w1")
    (attempt,) = _attempts(sc.contact_id)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT and attempt.provider_call_id is None
    run = next(_RUN_IDS)

    completed = _post_webhook(real_client, attempt, run, "user_hangup")
    assert completed.json()["outcome"] == "ended_normally"

    done = _attempts(sc.contact_id)[0]
    assert done.state == CallAttemptState.ENDED_NORMALLY
    assert done.provider_call_id == str(run)  # the run is now known
    assert _sessions_and_analysis(attempt.id) == (1, 1)

    # The already-scheduled retry is now stale: it must not dial.
    sc.queue.enqueue(
        DialJob.new(campaign_id=sc.campaign_id, contact_id=sc.contact_id, attempt_number=2)
    )
    outcome = _process(sc, redis_client, provider, sc.queue.read_one("w3", 100))
    assert outcome != JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 1 and len(_attempts(sc.contact_id)) == 1


def test_never_connected_webhook_for_an_ambiguous_attempt_still_retries_once(
    fake, redis_client, provider, real_client
):
    sc = _first_attempt_ambiguous(fake, redis_client, provider, "w2")
    (attempt,) = _attempts(sc.contact_id)
    run = next(_RUN_IDS)

    unanswered = _post_webhook(real_client, attempt, run, "no_answer")
    assert unanswered.json()["outcome"] == "never_connected"

    first = _attempts(sc.contact_id)[0]
    assert first.state == CallAttemptState.FAILED_TO_CONNECT and first.provider_call_id == str(run)
    assert _sessions_and_analysis(attempt.id) == (0, 0)  # never connected

    sc.queue.enqueue(
        DialJob.new(campaign_id=sc.campaign_id, contact_id=sc.contact_id, attempt_number=2)
    )
    assert _process(sc, redis_client, provider, sc.queue.read_one("w3", 100)) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    assert fake.calls == 2 and [a.attempt_number for a in _attempts(sc.contact_id)] == [1, 2]


def test_a_definite_failure_stays_final_a_late_webhook_does_not_reopen_it(
    fake, redis_client, provider, real_client
):
    """Only an UNRESOLVED AMBIGUOUS trigger is provisional. A definite
    provider rejection is final, as before."""
    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "w4"), _admission(redis_client)
    fake.error = DograhApiError(400, "bad", category=DograhErrorCategory.VALIDATION_ERROR)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    _drive(queue, admission, provider, redis_client, *queue.read_one("w1", 100))
    (attempt,) = _attempts(contact_id)

    response = _post_webhook(real_client, attempt, next(_RUN_IDS), "user_hangup")

    assert response.json()["outcome"] == "already_processed"
    assert _attempts(contact_id)[0].state == CallAttemptState.FAILED_TO_CONNECT
