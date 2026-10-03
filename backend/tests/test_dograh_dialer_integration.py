"""Checkpoint 08/09 -- calling_engine="dograh" branch in
app/services/queue/dialer_worker.py::_place_call.
"""

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhErrorCategory,
    DograhTriggerResult,
)


def _setup(db_session, *, phone="555-950-0001"):
    campaign = Campaign(name="Dograh dialer test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    db_session.flush()
    return campaign, contact


def _queue(redis_client) -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, "test:dograh:calls", "test:dograh:dialer-workers")


def _admission(redis_client) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )


def _with_dograh_engine(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()


def _run(db_session, redis_client, provider, monkeypatch, campaign, contact, *, fake_client):
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    breaker = CircuitBreaker(redis_client, "mock")  # native breaker -- unused on this path
    monkeypatch.setattr(
        "app.services.telephony.factory.get_dograh_client", lambda: fake_client
    )
    try:
        outcome = process_one_job(
            db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
        )
        db_session.commit()
        return outcome
    finally:
        get_settings.cache_clear()


def test_successful_trigger_leaves_state_initiated_not_connected(
    db_session, redis_client, provider, monkeypatch
):
    """Checkpoint 09 §2: provider acceptance != phone connection --
    Dograh only confirms the job was queued, so attempt.state must NOT
    jump to CONNECTED and contact.status must NOT jump to
    IN_CONVERSATION on trigger success alone. Only the completion
    webhook (dograh_webhook_service.py) has enough information to know
    whether the call actually connected."""
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session)

    outcome = _run(
        db_session,
        redis_client,
        provider,
        monkeypatch,
        campaign,
        contact,
        fake_client=_FakeDograhClient(result=DograhTriggerResult(99, "WR-99")),
    )

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
    assert attempt.state == CallAttemptState.INITIATED
    assert attempt.provider == "dograh"
    assert attempt.provider_call_id == "99"
    db_session.refresh(contact)
    assert contact.status == ContactStatus.DIALING

    event = (
        db_session.query(CallEvent)
        .filter(
            CallEvent.call_attempt_id == attempt.id, CallEvent.event_type == "DOGRAH_CALL_TRIGGERED"
        )
        .one()
    )
    assert event.payload["workflow_run_id"] == 99


def test_trigger_failure_marks_failed_to_connect_and_does_not_crash(
    db_session, redis_client, provider, monkeypatch
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="555-950-0002")

    outcome = _run(
        db_session,
        redis_client,
        provider,
        monkeypatch,
        campaign,
        contact,
        fake_client=_FakeDograhClient(
            error=DograhApiError(
                400, "Telephony not configured", category=DograhErrorCategory.VALIDATION_ERROR
            )
        ),
    )

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert attempt.provider == "dograh"

    event = (
        db_session.query(CallEvent)
        .filter(
            CallEvent.call_attempt_id == attempt.id, CallEvent.event_type == "DOGRAH_TRIGGER_FAILED"
        )
        .one()
    )
    assert event.payload["category"] == "validation_error"


def test_ambiguous_timeout_is_tagged_distinctly_and_not_retried_immediately(
    db_session, redis_client, provider, monkeypatch
):
    """Checkpoint 09 §1.3: a timeout after the request may have reached
    Dograh must never be silently treated the same as a confirmed
    rejection -- it's tagged with its own event type so it's
    observable/distinguishable, and it still only ever goes through
    RecoveryManager's normal backoff, never an immediate re-trigger."""
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="555-950-0003")

    outcome = _run(
        db_session,
        redis_client,
        provider,
        monkeypatch,
        campaign,
        contact,
        fake_client=_FakeDograhClient(
            error=DograhApiError(
                0, "timed out waiting for response", category=DograhErrorCategory.AMBIGUOUS_REQUEST
            )
        ),
    )

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT

    event = (
        db_session.query(CallEvent)
        .filter(
            CallEvent.call_attempt_id == attempt.id,
            CallEvent.event_type == "DOGRAH_TRIGGER_AMBIGUOUS",
        )
        .one()
    )
    assert event.payload["category"] == "ambiguous_request"
    # Still exactly one attempt -- no second call_attempt row was
    # created synchronously as a result of the ambiguity.
    assert (
        db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).count() == 1
    )


def test_dograh_circuit_breaker_is_independent_of_native_provider(db_session, redis_client):
    """Checkpoint 09 §7: the dograh circuit breaker must be its own
    key, never shared with the native mock provider's breaker."""
    dograh_breaker = CircuitBreaker(redis_client, "dograh")
    native_breaker = CircuitBreaker(redis_client, "mock")

    for _ in range(dograh_breaker.error_threshold):
        dograh_breaker.record_failure()

    assert dograh_breaker.is_open() is True
    assert native_breaker.is_open() is False


class _FakeDograhClient:
    def __init__(
        self, *, result: DograhTriggerResult | None = None, error: Exception | None = None
    ):
        self.result = result
        self.error = error

    def find_runs_for_attempt(self, call_attempt_id, since):
        return []  # reconciliation: Dograh has no run for this attempt

    def trigger_call(self, *, phone_number, initial_context):
        if self.error:
            raise self.error
        assert "call_attempt_id" in initial_context
        return self.result
