"""Checkpoint 08 -- calling_engine="dograh" branch in
app/services/queue/dialer_worker.py::_place_call."""

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhApiError, DograhTriggerResult


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


def test_successful_trigger_records_provider_acceptance_but_not_connection(
    db_session, redis_client, provider, monkeypatch
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session)
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    breaker = CircuitBreaker(redis_client, "dograh")

    monkeypatch.setattr(
        "app.services.telephony.factory.get_dograh_client",
        lambda: _FakeDograhClient(result=DograhTriggerResult(99, "WR-99")),
    )

    try:
        outcome = process_one_job(
            db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
        )
        db_session.commit()

        assert outcome == JobOutcome.ADMITTED_AND_DIALED
        attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
        # CP09: Dograh accepting the request is not a phone connection.
        assert attempt.state == CallAttemptState.INITIATED
        assert attempt.provider == "dograh"
        assert attempt.provider_call_id == "99"
        db_session.refresh(contact)
        assert contact.status == ContactStatus.DIALING
    finally:
        get_settings.cache_clear()


def test_trigger_failure_marks_failed_to_connect_and_does_not_crash(
    db_session, redis_client, provider, monkeypatch
):
    _with_dograh_engine(monkeypatch)
    campaign, contact = _setup(db_session, phone="555-950-0002")
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    breaker = CircuitBreaker(redis_client, "dograh")

    monkeypatch.setattr(
        "app.services.telephony.factory.get_dograh_client",
        lambda: _FakeDograhClient(error=DograhApiError(400, "Telephony not configured")),
    )

    try:
        outcome = process_one_job(
            db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
        )
        db_session.commit()

        assert outcome == JobOutcome.ADMITTED_AND_DIALED
        attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
        assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
        assert attempt.provider == "dograh"
    finally:
        get_settings.cache_clear()


class _FakeDograhClient:
    def __init__(
        self, *, result: DograhTriggerResult | None = None, error: Exception | None = None
    ):
        self.result = result
        self.error = error

    def trigger_call(self, *, phone_number, initial_context):
        if self.error:
            raise self.error
        assert "call_attempt_id" in initial_context
        return self.result
