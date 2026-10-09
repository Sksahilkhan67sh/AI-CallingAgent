"""Checkpoint 10 -- provider failure injection through the real dialer worker.

For every provider-boundary failure: the attempt state, the audit/call event,
the circuit breaker, the queue ack and -- above all -- that Dograh's trigger is
never called a second time for the same attempt. The Dograh client is a
deterministic fake; nothing here is evidence about a real Dograh instance."""

import pytest

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.job import DialJob
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhConfigurationError,
    DograhErrorCategory,
    DograhTriggerResult,
)
from tests.test_dograh_dialer_integration import (
    _admission,
    _queue,
    _setup,
    _with_dograh_engine,
)

C = DograhErrorCategory
GROUP = "test:dograh:dialer-workers"
STREAM = "test:dograh:calls"


class _CountingClient:
    def __init__(self, *, result=None, error=None):
        self.result, self.error, self.trigger_calls = result, error, 0

    def find_runs_for_attempt(self, call_attempt_id, since):
        return []

    def trigger_call(self, *, phone_number, initial_context):
        self.trigger_calls += 1
        if self.error:
            raise self.error
        return self.result


def _pending(redis_client) -> int:
    return redis_client.xpending(STREAM, GROUP)["pending"]


def _deliver(db, redis_client, provider, monkeypatch, campaign, contact, getter):
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", getter)
    outcome = process_one_job(
        db,
        queue,
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, "mock"),
        consumer_name="w1",
    )
    db.commit()
    return outcome


def _events(db, attempt):
    rows = db.query(CallEvent).filter(CallEvent.call_attempt_id == attempt.id)
    return {e.event_type: e for e in rows}


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    get_settings.cache_clear()


DEFINITE = [
    (401, C.AUTHENTICATION_ERROR),
    (404, C.PROVIDER_REJECTED),
    (422, C.VALIDATION_ERROR),
    (429, C.RATE_LIMITED),
    (503, C.PROVIDER_UNAVAILABLE),
    (0, C.CONNECTION_ERROR),
]
AMBIGUOUS = [
    (500, C.AMBIGUOUS_REQUEST),
    (504, C.AMBIGUOUS_REQUEST),
    (0, C.AMBIGUOUS_REQUEST),  # read timeout / connection lost after send
    (0, C.TIMEOUT),
]


@pytest.mark.parametrize("status,category", DEFINITE + AMBIGUOUS)
def test_provider_failure_is_recorded_acked_counted_and_never_retriggered(
    db_session, redis_client, provider, monkeypatch, status, category
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone=f"989-990-{status:04d}"[:12])
    fake = _CountingClient(error=DograhApiError(status, "boom", category=category))

    outcome = _deliver(
        db_session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake
    )

    attempt = db_session.query(CallAttempt).filter_by(contact_id=contact.id).one()
    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert attempt.provider_call_id is None
    ambiguous = category in (C.AMBIGUOUS_REQUEST, C.TIMEOUT)
    events = _events(db_session, attempt)
    assert ("DOGRAH_TRIGGER_AMBIGUOUS" in events) == ambiguous
    assert ("DOGRAH_TRIGGER_FAILED" in events) == (not ambiguous)
    recorded = events["DOGRAH_TRIGGER_AMBIGUOUS" if ambiguous else "DOGRAH_TRIGGER_FAILED"]
    assert recorded.payload["category"] == category.value
    assert "api" not in str(recorded.payload).lower() or "key" not in str(recorded.payload).lower()
    assert _pending(redis_client) == 0  # acked only after the durable commit
    assert redis_client.get("circuit:dograh:errors") == "1"  # counted against Dograh's breaker

    # The same job is delivered again (reclaim / duplicate): never a second trigger.
    _deliver(db_session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake)
    assert fake.trigger_calls == 1
    assert db_session.query(CallAttempt).filter_by(contact_id=contact.id).count() == 1


def test_unexpected_exception_leaves_job_pending_and_never_retriggers(
    redis_client, provider, monkeypatch
):
    """Real committed sessions (the shared db_session cannot model the worker's
    rollback): the durable claim written before the trigger must survive it."""
    from tests.test_concurrency_dialer import _Session

    _with_dograh_engine(monkeypatch)
    setup = _Session()
    campaign, contact = _setup(setup, phone="989-991-0001")
    setup.commit()
    fake = _CountingClient(error=RuntimeError("unclassified bug"))

    session = _Session()
    try:
        with pytest.raises(RuntimeError):
            _deliver(session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake)
    finally:
        session.close()
    assert _pending(redis_client) == 1  # not acked: a reclaim re-drives it

    check = _Session()
    attempt = check.query(CallAttempt).filter_by(contact_id=contact.id).one()
    assert attempt.provider_call_id is None  # the claim survived the rollback
    check.close()

    session = _Session()
    try:
        _deliver(session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake)
    finally:
        session.close()
    assert fake.trigger_calls == 1  # the claim blocks a blind second trigger
    setup.close()


def test_configuration_error_is_audited_and_does_not_strand_the_contact(
    db_session, redis_client, provider, monkeypatch
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="989-991-0002")

    def _unconfigured():
        raise DograhConfigurationError("DOGRAH_API_KEY and DOGRAH_TRIGGER_UUID must both be set")

    _deliver(db_session, redis_client, provider, monkeypatch, campaign, contact, _unconfigured)

    attempt = db_session.query(CallAttempt).filter_by(contact_id=contact.id).one()
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert _events(db_session, attempt)["DOGRAH_CONFIGURATION_ERROR"].payload == {
        "status_code": None,
        "category": "configuration_error",
        "reason_key": "provider_configuration_error",
    }
    db_session.refresh(contact)
    assert contact.status != ContactStatus.DIALING  # handed to RecoveryManager, not stuck
    assert redis_client.get("circuit:dograh:errors") is None  # not a provider-health signal
    assert _pending(redis_client) == 0


def test_open_circuit_blocks_the_trigger_and_leaves_the_job_for_later(
    db_session, redis_client, provider, monkeypatch
):
    """Rate-limit storms: after the breaker opens, admission refuses the job --
    no trigger, no ack, no new attempt row."""
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="989-991-0003")
    breaker = CircuitBreaker(redis_client, "dograh")
    for _ in range(breaker.error_threshold):
        breaker.record_failure()
    fake = _CountingClient(result=DograhTriggerResult(1, "WR-1"))

    outcome = _deliver(
        db_session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake
    )

    assert outcome == JobOutcome.NOT_ADMITTED
    assert fake.trigger_calls == 0
    assert _pending(redis_client) == 1
    assert db_session.query(CallAttempt).filter_by(contact_id=contact.id).count() == 0


def test_accepted_trigger_is_initiated_not_connected_and_records_the_run(
    db_session, redis_client, provider, monkeypatch
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="989-991-0004")
    fake = _CountingClient(result=DograhTriggerResult(8123, "WR-8123"))

    _deliver(db_session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake)

    attempt = db_session.query(CallAttempt).filter_by(contact_id=contact.id).one()
    assert (attempt.state, attempt.provider, attempt.provider_call_id) == (
        CallAttemptState.INITIATED,
        "dograh",
        "8123",
    )
    db_session.refresh(contact)
    assert contact.status == ContactStatus.DIALING and _pending(redis_client) == 0


def test_rate_limit_responses_cannot_become_a_retry_storm(
    db_session, redis_client, provider, monkeypatch
):
    """Consecutive 429s: each is one definite failure recorded for RecoveryManager,
    and once the breaker threshold is reached admission stops the dialer from
    calling Dograh at all -- the trigger count is bounded by the threshold, however
    many jobs are queued."""
    _with_dograh_engine(monkeypatch)
    threshold = CircuitBreaker(redis_client, "dograh").error_threshold
    fake = _CountingClient(error=DograhApiError(429, "slow down", category=C.RATE_LIMITED))
    outcomes = []
    for i in range(threshold + 3):
        campaign, contact = _setup(db_session, phone=f"989-992-{i:04d}")
        outcomes.append(
            _deliver(
                db_session, redis_client, provider, monkeypatch, campaign, contact, lambda: fake
            )
        )

    assert fake.trigger_calls == threshold  # no further Dograh calls once the breaker opened
    assert outcomes[:threshold] == [JobOutcome.ADMITTED_AND_DIALED] * threshold
    assert all(o == JobOutcome.NOT_ADMITTED for o in outcomes[threshold:])
    assert _pending(redis_client) == 3  # refused jobs stay queued: not acked, not lost
    failed = db_session.query(CallEvent).filter_by(event_type="DOGRAH_TRIGGER_FAILED").all()
    assert len(failed) == threshold
    assert {e.payload["category"] for e in failed} == {"rate_limited"}
