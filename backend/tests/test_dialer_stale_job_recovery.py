"""Checkpoint 09 §4.3 -- a job left unacked by a crashed dialer worker
must actually be reprocessed once reclaimed, not just have its Redis
Streams ownership silently transferred. Mirrors the equivalent
Checkpoint 06 fix/test for the analysis queue.
"""

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker


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


def test_stale_job_left_unacked_is_reprocessed_after_reclaim(db_session, redis_client, provider):
    """Simulates a worker that read a job (claiming it in the consumer
    group) and then crashed before calling process_one_job -- the
    message is pending, unacked, owned by a now-dead consumer. A
    second worker must be able to reclaim AND actually execute it, not
    just take ownership."""
    campaign = Campaign(name="stale job recovery test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    phone = "555-980-0001"
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    db_session.commit()

    queue = RedisStreamQueue(redis_client, "test:stale:calls", "test:stale:dialer-workers")
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    # Worker A reads (claims) the job but "crashes" -- never calls
    # process_one_job, never acks.
    read = queue.read_one("worker-a", block_ms=100)
    assert read is not None

    # No CallAttempt exists yet -- worker A never got that far.
    assert db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).count() == 0

    # Worker B reclaims after the idle window and must actually dial,
    # not just take ownership of an unexecuted message.
    breaker = CircuitBreaker(redis_client, "mock")
    reclaimed = queue.reclaim_stale("worker-b", idle_ms=0)
    assert len(reclaimed) == 1
    message_id, job = reclaimed[0]

    outcome = process_claimed_job(
        db_session, queue, _admission(redis_client), provider, breaker, message_id, job
    )
    db_session.commit()

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    attempt = db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).one()
    assert attempt.state == CallAttemptState.CONNECTED

    # And it's fully acked -- no longer pending for anyone.
    pending = redis_client.xpending(queue.stream_key, queue.group)
    assert pending["pending"] == 0


def test_reclaimed_job_is_not_reprocessed_twice(db_session, redis_client, provider):
    """If the contact was somehow already handled by the time the
    reclaim runs (e.g. DialEligibilityService now says ineligible), the
    reclaimed job must be a safe no-op, never a duplicate dial."""
    campaign = Campaign(name="stale job dedup test", status=CampaignStatus.PAUSED)
    db_session.add(campaign)
    db_session.flush()
    phone = "555-980-0002"
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    db_session.commit()

    queue = RedisStreamQueue(redis_client, "test:stale2:calls", "test:stale2:dialer-workers")
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    queue.read_one("worker-a", block_ms=100)  # claim + "crash"

    breaker = CircuitBreaker(redis_client, "mock")
    reclaimed = queue.reclaim_stale("worker-b", idle_ms=0)
    message_id, job = reclaimed[0]

    outcome = process_claimed_job(
        db_session, queue, _admission(redis_client), provider, breaker, message_id, job
    )
    db_session.commit()

    # Campaign is paused -- DialEligibilityService's fresh re-check
    # (already existing CP03 behavior) must skip the dial entirely.
    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).count() == 0
