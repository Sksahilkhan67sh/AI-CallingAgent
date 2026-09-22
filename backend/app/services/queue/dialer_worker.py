"""The calling worker -- Checkpoint 03 Steps 5, 15-19, 22.

Processing contract for one job:

  read -> admission (rate/concurrency/circuit) -> load DB state ->
  final eligibility -> claim/create CallAttempt -> dial via provider ->
  persist outcome -> release admission slot -> ack

Nothing is acked before the outcome is durably persisted (Step 5). A
job that fails admission is left unacked so it can be retried by this
or another worker shortly (backpressure -- Step 10) rather than being
dropped.
"""

import logging
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.models.retry_policy import RetryPolicy
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services.eligibility_service import DialEligibilityService
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony.base import ProviderOutcome, TelephonyProvider
from app.services.telephony.circuit_breaker import CircuitBreaker

logger = logging.getLogger("dialer_worker")

# Backpressure bound (Step 10): how long/how many times to wait for
# admission capacity before giving up on this delivery and leaving it
# unacked for a later retry, rather than looping forever inside one
# process call.
_ADMISSION_RETRY_ATTEMPTS = 5
_ADMISSION_RETRY_SLEEP_SECONDS = 0.2


class JobOutcome:
    ADMITTED_AND_DIALED = "admitted_and_dialed"
    NOT_ADMITTED = "not_admitted"  # left unacked -- backpressure
    NOT_ELIGIBLE = "not_eligible"  # acked -- correctly skipped
    ALREADY_PROCESSED = "already_processed"  # acked -- duplicate delivery, safely ignored
    NO_JOB = "no_job"


def process_one_job(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    *,
    consumer_name: str,
    block_ms: int = 1000,
) -> str:
    read = queue.read_one(consumer_name, block_ms)
    if read is None:
        return JobOutcome.NO_JOB

    message_id, job = read
    return _process(db, queue, admission, provider, circuit_breaker, message_id, job)


def _process(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    message_id: str,
    job: DialJob,
) -> str:
    admitted = False
    for _ in range(_ADMISSION_RETRY_ATTEMPTS):
        result = admission.try_admit(campaign_id=job.campaign_id, provider_name=provider.name)
        if result.admitted:
            admitted = True
            break
        time.sleep(_ADMISSION_RETRY_SLEEP_SECONDS)

    if not admitted:
        logger.info(
            "admission_backpressure", extra={"job_id": job.job_id, "trace_id": job.trace_id}
        )
        return JobOutcome.NOT_ADMITTED  # left unacked on purpose

    try:
        return _dial(db, provider, circuit_breaker, job)
    finally:
        admission.release(campaign_id=job.campaign_id, provider_name=provider.name)
        queue.ack(message_id)


def _dial(
    db: Session, provider: TelephonyProvider, circuit_breaker: CircuitBreaker, job: DialJob
) -> str:
    contacts = ContactRepository(db)
    campaigns = CampaignRepository(db)
    attempts = CallAttemptRepository(db)
    suppressions = SuppressionRepository(db)

    contact = contacts.get_by_id(uuid.UUID(job.contact_id))
    campaign = campaigns.get_by_id(uuid.UUID(job.campaign_id))
    if contact is None or campaign is None:
        logger.warning("job_target_missing", extra={"job_id": job.job_id})
        return JobOutcome.NOT_ELIGIBLE

    # Idempotency (Step 4) is checked before re-validating eligibility,
    # deliberately: a duplicate delivery of a job already fully handled
    # isn't a new eligibility question -- by the time a second delivery
    # arrives, the contact's status has likely already moved on (e.g. to
    # InConversation), which would otherwise make it look "ineligible"
    # rather than "already done." "Already done" must win.
    existing = attempts.get_by_contact_and_number(contact.id, job.attempt_number)
    if existing is not None and existing.provider_call_id is not None:
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED

    retry_policy = db.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign.id)
    ).scalar_one_or_none()

    eligibility = DialEligibilityService(suppressions).check(
        contact, campaign, retry_policy, now=datetime.now(UTC)
    )
    if not eligibility.eligible:
        logger.info(
            "dial_not_eligible",
            extra={"job_id": job.job_id, "reason": eligibility.reason},
        )
        return JobOutcome.NOT_ELIGIBLE

    attempt, created = attempts.get_or_create(contact.id, job.attempt_number)

    if not created and attempt.provider_call_id is not None:
        # Lost a race with another delivery/worker between the
        # idempotency check above and claiming here.
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED

    if created:
        contact.attempt_count += 1
    contact.status = ContactStatus.DIALING
    db.flush()

    _place_call(db, provider, circuit_breaker, contact, attempt, job)
    return JobOutcome.ADMITTED_AND_DIALED


def _place_call(
    db: Session,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    contact: Contact,
    attempt: CallAttempt,
    job: DialJob,
) -> None:
    result = provider.create_outbound_call(
        to_number=contact.normalized_phone_number, idempotency_key=job.idempotency_key
    )

    if result.outcome == ProviderOutcome.AMBIGUOUS:
        # Step 19: never assume a timeout means "no call was placed" --
        # query the provider's own record of the idempotency key/call
        # before deciding anything.
        result = provider.find_call_by_idempotency_key(job.idempotency_key)

    if result.outcome == ProviderOutcome.INITIATED:
        circuit_breaker.record_success()
        attempt.provider = provider.name
        attempt.provider_call_id = result.provider_call_id
        attempt.state = CallAttemptState.CONNECTED
        contact.status = ContactStatus.IN_CONVERSATION
        db.flush()
        _start_conversation_safely(
            db,
            attempt,
            contact,
            is_reconnect=job.recovery_type is not None,
            previous_attempt_id=job.previous_attempt_id,
        )
    else:
        circuit_breaker.record_failure()
        attempt.provider = provider.name
        attempt.state = CallAttemptState.FAILED_TO_CONNECT
        attempt.connection_failure_reason = result.failure_reason
        attempt.ended_at = datetime.now(UTC)
        db.flush()
        if result.failure_reason is not None:
            _handle_never_connected_failure(db, attempt, contact, result.failure_reason)

    db.flush()


def _start_conversation_safely(
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    *,
    is_reconnect: bool = False,
    previous_attempt_id: str | None = None,
) -> None:
    """Checkpoint 04 Step 41-42 (extended in Checkpoint 05 with
    is_reconnect/previous_attempt_id for retries): initialize the AI
    conversation once the call connects. The call itself already
    connected successfully (attempt.state is already CONNECTED)
    regardless of what happens here, so a failure in conversation
    startup is logged, not raised -- it must not roll back or corrupt
    the telephony-layer fact that the call connected.
    """
    from app.services.ai.conversation.start import start_conversation

    try:
        start_conversation(
            db,
            attempt,
            contact,
            is_reconnect=is_reconnect,
            previous_attempt_id=previous_attempt_id,
        )
    except Exception:
        logger.exception(
            "conversation_start_failed", extra={"attempt_id": str(attempt.id)}
        )


def _handle_never_connected_failure(
    db: Session, attempt: CallAttempt, contact: Contact, reason: NeverConnectedFailureReason
) -> None:
    """Checkpoint 05: a never-connected failure (no_answer, busy, etc.)
    also goes through the single RecoveryManager -- Checkpoint 03 left
    this as a documented gap ("retry-policy evaluation is a later
    checkpoint's job"); this is that checkpoint. Logged, not raised, for
    the same reason as conversation-start failures above: the call
    outcome itself is already durably persisted regardless of what the
    recovery decision does.
    """
    try:
        campaign = db.get(Campaign, contact.campaign_id)
        if campaign is not None:
            RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
                attempt, contact, campaign, never_connected=True, reason_key=reason.value
            )
    except Exception:
        logger.exception("recovery_handling_failed", extra={"attempt_id": str(attempt.id)})
