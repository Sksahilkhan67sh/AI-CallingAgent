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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.models.retry_policy import RetryPolicy
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services import call_state
from app.services.audit_service import record_audit_event
from app.services.eligibility_service import DialEligibilityService
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony.base import ProviderOutcome, TelephonyProvider
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhApiError, ProviderErrorKind

logger = logging.getLogger("dialer_worker")

# Backpressure bound (Step 10): how long/how many times to wait for
# admission capacity before giving up on this delivery and leaving it
# unacked for a later retry, rather than looping forever inside one
# process call.
_ADMISSION_RETRY_ATTEMPTS = 5
_ADMISSION_RETRY_SLEEP_SECONDS = 0.2

DOGRAH_PROVIDER = "dograh"
_DLQ_ACTOR = "dialer-worker"


class JobOutcome:
    ADMITTED_AND_DIALED = "admitted_and_dialed"
    NOT_ADMITTED = "not_admitted"  # left unacked -- backpressure
    NOT_ELIGIBLE = "not_eligible"  # acked -- correctly skipped
    ALREADY_PROCESSED = "already_processed"  # acked -- duplicate delivery, safely ignored
    DEAD_LETTERED = "dead_lettered"  # acked -- poison job parked in the DLQ
    NO_JOB = "no_job"


def admission_provider_name(provider: TelephonyProvider) -> str:
    """Admission/circuit state is keyed by the provider that actually places
    the call: Dograh when calling_engine=dograh, not the placeholder mock."""
    return DOGRAH_PROVIDER if get_settings().calling_engine == "dograh" else provider.name


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


def process_reclaimed(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    reclaimed: list[tuple[str, DialJob]],
) -> int:
    """Jobs a crashed worker left pending. They go through the exact same
    pipeline as fresh ones -- including the DB idempotency check, so a call
    the dead worker already placed is never placed twice."""
    handled = 0
    for message_id, job in reclaimed:
        try:
            _process(db, queue, admission, provider, circuit_breaker, message_id, job)
            handled += 1
        except Exception:
            logger.exception("reclaimed_job_failed", extra={"job_id": job.job_id})
    return handled


def _process(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    message_id: str,
    job: DialJob,
) -> str:
    """read -> admit -> load/validate -> execute -> COMMIT -> ack.

    The ack happens strictly after the database commit: a crash anywhere
    before it leaves the job pending for reclaim, and reclaim is safe
    because the attempt row (with its `provider` intent marker) was
    committed before any call was placed."""
    provider_name = admission_provider_name(provider)
    admitted = False
    for _ in range(_ADMISSION_RETRY_ATTEMPTS):
        result = admission.try_admit(campaign_id=job.campaign_id, provider_name=provider_name)
        if result.admitted:
            admitted = True
            break
        time.sleep(_ADMISSION_RETRY_SLEEP_SECONDS)

    if not admitted:
        logger.info(
            "admission_backpressure",
            extra={"job_id": job.job_id, "correlation_id": job.trace_id},
        )
        return JobOutcome.NOT_ADMITTED  # left unacked on purpose

    try:
        outcome = _dial(db, provider, circuit_breaker, job)
        if outcome == JobOutcome.NOT_ADMITTED:
            db.rollback()
            return outcome  # DB-side capacity full: leave unacked, same as above
        db.commit()
    except Exception:
        db.rollback()
        metrics.incr(metrics.WORKER_FAILURES)
        failures = queue.record_failure(message_id)
        logger.exception(
            "job_processing_failed",
            extra={"job_id": job.job_id, "correlation_id": job.trace_id, "failures": failures},
        )
        if failures >= get_settings().queue_max_deliveries:
            _dead_letter(db, queue, message_id, job, reason="max_processing_failures")
            return JobOutcome.DEAD_LETTERED
        raise  # leave unacked: reclaimed and retried later
    finally:
        admission.release(campaign_id=job.campaign_id, provider_name=provider_name)

    queue.clear_failures(message_id)
    queue.ack(message_id)
    return outcome


def _dead_letter(
    db: Session, queue: RedisStreamQueue, message_id: str, job: DialJob, *, reason: str
) -> None:
    """Durable DLQ record in PostgreSQL (AuditLog) + the parked job on the
    DLQ stream. Nothing is dialed for a dead-lettered job."""
    try:
        record_audit_event(
            db,
            actor=_DLQ_ACTOR,
            action="queue.dead_lettered",
            entity_type="contact",
            entity_id=uuid.UUID(job.contact_id),
            metadata={
                "campaign_id": job.campaign_id,
                "contact_id": job.contact_id,
                "attempt_number": job.attempt_number,
                "job_id": job.job_id,
                "correlation_id": job.trace_id,
                "reason": reason,
                "retry_count": get_settings().queue_max_deliveries,
                "enqueued_at": job.enqueued_at,
                "dead_lettered_at": datetime.now(UTC).isoformat(),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("dlq_audit_write_failed", extra={"job_id": job.job_id})
    queue.dead_letter(
        message_id, job, dlq_key=get_settings().queue_dlq_stream_key, reason=reason
    )
    metrics.incr(metrics.DLQ_ENTRIES)


def _dograh_capacity_available(db: Session, campaign_id: uuid.UUID) -> bool:
    """Concurrency for Dograh calls is counted from PostgreSQL (the source
    of truth): a Dograh call stays in flight for minutes, long after the
    short Redis admission reservation is released, and a Redis counter
    would drift on a crash. Slight overshoot under a race between workers
    is bounded by the worker count."""
    settings = get_settings()
    in_flight = (CallAttempt.provider == DOGRAH_PROVIDER) & CallAttempt.state.in_(
        (CallAttemptState.INITIATED, CallAttemptState.CONNECTED)
    )
    total = db.execute(select(func.count()).select_from(CallAttempt).where(in_flight)).scalar_one()
    if total >= settings.global_concurrency_limit:
        return False
    per_campaign = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .join(Contact, Contact.id == CallAttempt.contact_id)
        .where(in_flight, Contact.campaign_id == campaign_id)
    ).scalar_one()
    return bool(per_campaign < settings.campaign_concurrency_limit)


def _dial(
    db: Session, provider: TelephonyProvider, circuit_breaker: CircuitBreaker, job: DialJob
) -> str:
    contacts = ContactRepository(db)
    campaigns = CampaignRepository(db)
    attempts = CallAttemptRepository(db)
    suppressions = SuppressionRepository(db)
    dograh = get_settings().calling_engine == "dograh"

    contact = contacts.get_by_id(uuid.UUID(job.contact_id))
    campaign = campaigns.get_by_id(uuid.UUID(job.campaign_id))
    if contact is None or campaign is None:
        logger.warning("job_target_missing", extra={"job_id": job.job_id})
        return JobOutcome.NOT_ELIGIBLE

    # Idempotency (Step 4) is checked before re-validating eligibility,
    # deliberately: "already done" must win over "no longer eligible".
    # For Dograh the `provider` column is the intent marker written and
    # committed BEFORE the trigger request, so an attempt that has it -- even
    # without a run id (ambiguous or crashed mid-request) -- must never be
    # triggered again; the reconciler owns it from there.
    existing = attempts.get_by_contact_and_number(contact.id, job.attempt_number)
    if existing is not None and (
        existing.provider_call_id is not None or (dograh and existing.provider == DOGRAH_PROVIDER)
    ):
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED

    retry_policy = db.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign.id)
    ).scalar_one_or_none()

    # Re-validate everything at dial time -- never trust eligibility
    # computed when the job was enqueued (campaign paused, contact
    # suppressed, window closed, retry budget spent, since).
    if retry_policy is not None and job.attempt_number > retry_policy.max_retries + 1:
        logger.info("dial_not_eligible", extra={"job_id": job.job_id, "reason": "retry_exhausted"})
        return JobOutcome.NOT_ELIGIBLE

    eligibility = DialEligibilityService(suppressions).check(
        contact, campaign, retry_policy, now=datetime.now(UTC)
    )
    if not eligibility.eligible:
        logger.info(
            "dial_not_eligible",
            extra={"job_id": job.job_id, "reason": eligibility.reason},
        )
        return JobOutcome.NOT_ELIGIBLE

    if dograh and existing is None and not _dograh_capacity_available(db, campaign.id):
        logger.info("dograh_capacity_full", extra={"job_id": job.job_id})
        return JobOutcome.NOT_ADMITTED

    attempt, created = attempts.get_or_create(contact.id, job.attempt_number)

    if not created and (
        attempt.provider_call_id is not None or (dograh and attempt.provider == DOGRAH_PROVIDER)
    ):
        # Lost a race with another delivery/worker between the
        # idempotency check above and claiming here.
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED

    if created:
        contact.attempt_count += 1
    contact.status = ContactStatus.DIALING
    if dograh:
        # Durable intent BEFORE the request leaves this process: if we crash
        # or time out after this point the attempt is known-maybe-placed.
        attempt.provider = DOGRAH_PROVIDER
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type="PROVIDER_REQUEST_INTENT",
                payload={"correlation_id": job.trace_id, "attempt_number": job.attempt_number},
            )
        )
        db.commit()
    else:
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
    if get_settings().calling_engine == "dograh":
        # Checkpoint 08: telephony + STT + LLM + TTS all happen inside
        # Dograh's own pipeline once triggered -- there is no live
        # audio connection for our own ConversationOrchestrator to
        # drive, so _start_conversation_safely is deliberately never
        # called on this path. The call's outcome arrives later, once,
        # via app/api/routes/dograh_webhook.py.
        _place_call_via_dograh(db, contact, attempt, job, circuit_breaker)
        return

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


def _place_call_via_dograh(
    db: Session,
    contact: Contact,
    attempt: CallAttempt,
    job: DialJob,
    circuit_breaker: CircuitBreaker,
) -> None:
    """One HTTP call replaces both `create_outbound_call` and the
    conversation-start step: Dograh runs the whole call. `call_attempt_id`
    round-trips through Dograh's `initial_context` (verified: the trigger
    merges it into the run's context) and is how results correlate back.

    Provider acceptance is NOT a phone connection: on success the attempt
    stays INITIATED (contact DIALING) and only becomes CONNECTED from
    verified completion data (webhook or reconciler).
    """
    from app.services.telephony.dograh_client import DograhConfigurationError
    from app.services.telephony.factory import get_dograh_client

    campaign = db.get(Campaign, contact.campaign_id)
    initial_context = {
        "call_attempt_id": str(attempt.id),
        "contact_id": str(contact.id),
        "campaign_id": str(contact.campaign_id),
        "campaign_name": campaign.name if campaign is not None else "",
        "attempt_number": attempt.attempt_number,
        "correlation_id": job.trace_id,
    }
    log_ctx = {
        "attempt_id": str(attempt.id),
        "campaign_id": str(contact.campaign_id),
        "contact_id": str(contact.id),
        "correlation_id": job.trace_id,
        "provider": DOGRAH_PROVIDER,
    }

    started = time.monotonic()
    try:
        client = get_dograh_client()
        result = client.trigger_call(
            phone_number=contact.normalized_phone_number, initial_context=initial_context
        )
    except DograhConfigurationError:
        logger.exception("dograh_not_configured", extra=log_ctx)
        metrics.incr(metrics.DOGRAH_ERRORS)
        circuit_breaker.record_failure()
        _fail_attempt(db, attempt, contact, NeverConnectedFailureReason.PROVIDER_ERROR, "config")
        return
    except DograhApiError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        metrics.incr(metrics.DOGRAH_ERRORS)
        logger.warning(
            "dograh_trigger_failed",
            extra={
                **log_ctx,
                "error_kind": exc.kind.value,
                "status_code": exc.status_code,
                "ambiguous": exc.ambiguous,
                "latency_ms": latency_ms,
            },
        )
        if exc.kind != ProviderErrorKind.RATE_LIMITED:
            circuit_breaker.record_failure()
        if exc.ambiguous:
            # The request may have reached Dograh: a run may exist. Do NOT
            # create another call and do NOT involve RecoveryManager yet --
            # the reconciler decides once it has looked at Dograh.
            db.add(
                CallEvent(
                    call_attempt_id=attempt.id,
                    event_type="AMBIGUOUS_PROVIDER_STATE",
                    payload={"kind": exc.kind.value, "status_code": exc.status_code},
                )
            )
            db.flush()
            return
        reason = (
            NeverConnectedFailureReason.NETWORK_ERROR
            if exc.kind in (ProviderErrorKind.TIMEOUT, ProviderErrorKind.CONNECTION_ERROR)
            else NeverConnectedFailureReason.PROVIDER_ERROR
        )
        _fail_attempt(db, attempt, contact, reason, exc.kind.value)
        return

    circuit_breaker.record_success()
    attempt.provider_call_id = str(result.workflow_run_id)
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type="PROVIDER_ACCEPTED",
            payload={
                "workflow_run_id": result.workflow_run_id,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
    )
    db.flush()
    metrics.incr(metrics.CALLS_INITIATED)
    logger.info(
        "dograh_call_triggered", extra={**log_ctx, "workflow_run_id": result.workflow_run_id}
    )


def _fail_attempt(
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    reason: NeverConnectedFailureReason,
    detail: str,
) -> None:
    """A definite failure: Dograh confirmed (or it is certain) no call was
    placed. Hand the decision to RecoveryManager, the only retry owner."""
    call_state.transition(
        db, attempt, CallAttemptState.FAILED_TO_CONNECT, reason=detail, source="dialer_worker"
    )
    attempt.connection_failure_reason = reason
    attempt.ended_at = datetime.now(UTC)
    db.flush()
    metrics.incr(metrics.CALLS_FAILED)
    _handle_never_connected_failure(db, attempt, contact, reason)


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
