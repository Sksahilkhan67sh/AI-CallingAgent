"""The dialer worker -- Checkpoint 03 Steps 5-6, 15-19, 22-23, 36."""

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, NeverConnectedFailureReason
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.mock_provider import always_ambiguous, always_fails


def _setup(
    db_session,
    *,
    campaign_status=CampaignStatus.ACTIVE,
    contact_status=ContactStatus.PENDING,
    phone="555-500-0001"
):
    campaign = Campaign(name="Worker test campaign", status=campaign_status)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=contact_status,
    )
    db_session.add(contact)
    db_session.flush()
    return campaign, contact


def _queue(redis_client) -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, "test:calls", "test:dialer-workers")


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


def test_worker_claims_and_dials_a_job(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    breaker = CircuitBreaker(redis_client, provider.name)

    outcome = process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    db_session.refresh(contact)
    assert contact.status == ContactStatus.IN_CONVERSATION
    assert contact.attempt_count == 1


def test_worker_creates_exactly_one_call_attempt(db_session, redis_client, provider):
    from sqlalchemy import select

    from app.models.call_attempt import CallAttempt

    campaign, contact = _setup(db_session)
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    breaker = CircuitBreaker(redis_client, provider.name)

    process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    attempts = (
        db_session.execute(select(CallAttempt).where(CallAttempt.contact_id == contact.id))
        .scalars()
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1


def test_worker_handles_duplicate_delivery_without_dialing_twice(
    db_session, redis_client, provider
):
    campaign, contact = _setup(db_session)
    queue = _queue(redis_client)
    admission = _admission(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)

    job = DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1)
    queue.enqueue(job)
    process_one_job(db_session, queue, admission, provider, breaker, consumer_name="w1")

    # Simulate the same job being delivered again (e.g. after a crash
    # before ack, or a queue redelivery).
    queue.enqueue(job)
    outcome = process_one_job(db_session, queue, admission, provider, breaker, consumer_name="w1")

    assert outcome == JobOutcome.ALREADY_PROCESSED
    call_count = sum(1 for c in provider._calls.values())
    assert call_count == 1  # create_outbound_call effectively only resulted in one live call


def test_worker_acks_job_only_after_persistence(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    pending = redis_client.xpending("test:calls", "test:dialer-workers")
    assert pending["pending"] == 0


def test_worker_skips_suppressed_contact_without_dialing(db_session, redis_client, provider):
    from app.models.enums import SuppressionSource
    from app.models.suppression import Suppression

    campaign, contact = _setup(db_session, phone="555-500-0002")
    db_session.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opt-out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    outcome = process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert len(provider._calls) == 0


def test_worker_skips_inactive_campaign(db_session, redis_client, provider):
    campaign, contact = _setup(
        db_session, campaign_status=CampaignStatus.PAUSED, phone="555-500-0003"
    )
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    outcome = process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert len(provider._calls) == 0


def test_worker_skips_closed_contact(db_session, redis_client, provider):
    campaign, contact = _setup(
        db_session, contact_status=ContactStatus.CLOSED, phone="555-500-0004"
    )
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    outcome = process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    assert outcome == JobOutcome.NOT_ELIGIBLE


def test_worker_respects_calling_window(db_session, redis_client, provider):
    from datetime import time as dtime

    from app.models.retry_policy import RetryPolicy
    from app.repositories.suppression_repository import SuppressionRepository
    from app.services.eligibility_service import DialEligibilityService

    campaign, contact = _setup(db_session, phone="555-500-0005")
    policy = RetryPolicy(campaign_id=campaign.id, window_start=dtime(9, 0), window_end=dtime(10, 0))
    db_session.add(policy)
    db_session.flush()

    from datetime import UTC, datetime

    result = DialEligibilityService(SuppressionRepository(db_session)).check(
        contact, campaign, policy, now=datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    )

    assert result.eligible is False
    assert result.reason == "Outside calling window"


def test_provider_failure_persists_never_connected_reason(db_session, redis_client, provider):
    campaign, contact = _setup(db_session, phone="555-500-0006")
    provider.set_outcome(
        contact.normalized_phone_number, always_fails(NeverConnectedFailureReason.NO_ANSWER)
    )
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    db_session.refresh(contact)
    from sqlalchemy import select

    from app.models.call_attempt import CallAttempt

    attempt = db_session.execute(
        select(CallAttempt).where(CallAttempt.contact_id == contact.id)
    ).scalar_one()
    assert attempt.state.value == "FailedToConnect"
    assert attempt.connection_failure_reason == NeverConnectedFailureReason.NO_ANSWER
    assert attempt.ended_at is not None
    # Contact stays at Dialing -- see docs/CHECKPOINT-03-NOTES.md
    assert contact.status == ContactStatus.DIALING


def test_ambiguous_outcome_reconciles_to_existing_call(db_session, redis_client, provider):
    """Step 19/36: provider timed out, but the provider actually did
    create the call -- must NOT create a second one."""
    campaign, contact = _setup(db_session, phone="555-500-0007")
    job = DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1)
    provider.set_outcome(contact.normalized_phone_number, always_ambiguous)
    provider.simulate_provider_side_call_exists(job.idempotency_key, "mock-existing-call-123")

    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(job)

    process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    from sqlalchemy import select

    from app.models.call_attempt import CallAttempt

    attempt = db_session.execute(
        select(CallAttempt).where(CallAttempt.contact_id == contact.id)
    ).scalar_one()
    assert attempt.provider_call_id == "mock-existing-call-123"
    assert attempt.state.value == "Connected"


def test_ambiguous_outcome_reconciles_to_failure_when_call_never_existed(
    db_session, redis_client, provider
):
    campaign, contact = _setup(db_session, phone="555-500-0008")
    provider.set_outcome(contact.normalized_phone_number, always_ambiguous)
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    process_one_job(
        db_session, queue, _admission(redis_client), provider, breaker, consumer_name="w1"
    )

    from sqlalchemy import select

    from app.models.call_attempt import CallAttempt

    attempt = db_session.execute(
        select(CallAttempt).where(CallAttempt.contact_id == contact.id)
    ).scalar_one()
    assert attempt.state.value == "FailedToConnect"
    assert attempt.provider_call_id is None


def test_no_admission_capacity_leaves_job_unacked(db_session, redis_client, provider):
    campaign, contact = _setup(db_session, phone="555-500-0009")
    queue = _queue(redis_client)
    zero_capacity = AdmissionController(
        redis_client,
        global_cps_limit=100,
        campaign_cps_limit=100,
        provider_cps_limit=100,
        global_concurrency_limit=0,
        campaign_concurrency_limit=100,
        provider_concurrency_limit=100,
    )
    breaker = CircuitBreaker(redis_client, provider.name)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    outcome = process_one_job(
        db_session, queue, zero_capacity, provider, breaker, consumer_name="w1", block_ms=100
    )

    assert outcome == JobOutcome.NOT_ADMITTED
    pending = redis_client.xpending("test:calls", "test:dialer-workers")
    assert pending["pending"] == 1  # left unacked, not dropped
