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
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.models.retry_policy import RetryPolicy
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services.audit_service import record_audit_event
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
    # Dograh has no idempotency key, so a claimed-but-never-resolved attempt is
    # never re-triggered: it is routed through RecoveryManager as an ambiguous
    # trigger (acked -- the recovery job is now the durable record).
    AMBIGUOUS_RECOVERY = "ambiguous_recovery"
    # An attempt claim younger than the Dograh request timeouts may still be
    # in flight on another worker. Left UNACKED so a later reclaim re-checks.
    IN_FLIGHT = "in_flight"
    # Reconciliation (Dograh run listing) found the run an ambiguous trigger
    # had already created: it is adopted, no retry, the webhook drives it.
    RECONCILED = "reconciled_existing_run"
    # More than one run exists for one attempt: never pick one, never retry.
    RECONCILE_MULTIPLE = "reconcile_multiple_candidates"
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
    return process_claimed_job(db, queue, admission, provider, circuit_breaker, message_id, job)


def process_claimed_job(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    message_id: str,
    job: DialJob,
) -> str:
    """Checkpoint 09 §4.3 -- processes one already-claimed delivery, used
    both for a freshly read job (`process_one_job`) and for a job
    handed back by `RedisStreamQueue.reclaim_stale` (a worker that
    crashed after claiming it). Before this fix, `app/worker.py` called
    `reclaim_stale` (which does XAUTOCLAIM, transferring ownership) but
    never re-drove processing on what it returned -- the same
    unexercised gap Checkpoint 06 found and fixed for the analysis
    queue. A contact whose dialer worker crashed after claiming its job
    would otherwise sit stuck forever, appearing merely "in progress."

    Re-checks DB state via the normal `_dial` -> `DialEligibilityService`
    path below before executing anything (§4.3: "re-check DB state
    before execution") -- so a job reclaimed after the contact was
    already dialed by a *different* recovered path, or became
    ineligible in the meantime, is a safe no-op rather than a duplicate
    call.
    """
    from app.core.config import get_settings

    # Checkpoint 09 §5/§7: a Dograh-routed call must be admitted and
    # circuit-broken under its own "dograh" key, never silently sharing
    # the native provider's concurrency/CPS/circuit-breaker buckets --
    # they are different external dependencies with independent health.
    provider_name = "dograh" if get_settings().calling_engine == "dograh" else provider.name

    admitted = False
    for _ in range(_ADMISSION_RETRY_ATTEMPTS):
        result = admission.try_admit(campaign_id=job.campaign_id, provider_name=provider_name)
        if result.admitted:
            admitted = True
            break
        time.sleep(_ADMISSION_RETRY_SLEEP_SECONDS)

    if not admitted:
        logger.info(
            "admission_backpressure", extra={"job_id": job.job_id, "trace_id": job.trace_id}
        )
        return JobOutcome.NOT_ADMITTED  # left unacked on purpose

    # Ack contract (Step 5): the message is acked only AFTER the outcome is
    # durably committed. If anything fails before that (e.g. a transient
    # PostgreSQL error) nothing is acked, the transaction is rolled back, and
    # the job stays pending so a reclaim re-drives it -- a lost commit must
    # never become a lost job, or a placed call with no durable record.
    try:
        outcome = _dial(db, provider, circuit_breaker, job)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        admission.release(campaign_id=job.campaign_id, provider_name=provider_name)
    if outcome != JobOutcome.IN_FLIGHT:
        queue.ack(message_id)
    return outcome


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
    from app.core.config import get_settings

    # Dograh's trigger has no documented idempotency key, so for that engine
    # the existence of ANY attempt row for this (contact, attempt_number)
    # means a trigger was already attempted -- never re-trigger on it.
    is_dograh = get_settings().calling_engine == "dograh"

    existing = attempts.get_by_contact_and_number(contact.id, job.attempt_number)
    if existing is not None and existing.provider_call_id is not None:
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED
    if existing is not None and is_dograh:
        return _resolve_unresolved_dograh_claim(db, existing, contact, job)

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

    if not created and (attempt.provider_call_id is not None or is_dograh):
        # Lost a race with another delivery/worker between the
        # idempotency check above and claiming here.
        logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
        return JobOutcome.ALREADY_PROCESSED

    if created:
        contact.attempt_count += 1
    contact.status = ContactStatus.DIALING
    db.flush()

    outcome = _place_call(db, provider, circuit_breaker, contact, attempt, job)
    return outcome or JobOutcome.ADMITTED_AND_DIALED


def _resolve_unresolved_dograh_claim(
    db: Session, existing: CallAttempt, contact: Contact, job: DialJob
) -> str:
    """A CallAttempt exists for this job but never recorded a Dograh run.
    The claim is committed *before* the trigger request, so this means the
    worker died (or lost its commit) somewhere around the trigger: the call
    may or may not have been placed. Re-triggering could dial the contact
    twice, so it is treated exactly like an ambiguous trigger timeout --
    through RecoveryManager's backoff and eligibility re-checks."""
    from app.core.config import get_settings

    if existing.state != CallAttemptState.INITIATED:
        return JobOutcome.ALREADY_PROCESSED  # already resolved and routed to recovery

    settings = get_settings()
    in_flight_seconds = (
        settings.dograh_connect_timeout_seconds + settings.dograh_read_timeout_seconds + 5
    )
    if (datetime.now(UTC) - existing.started_at).total_seconds() < in_flight_seconds:
        logger.info("dograh_claim_possibly_in_flight", extra={"job_id": job.job_id})
        return JobOutcome.IN_FLIGHT

    logger.warning(
        "dograh_orphaned_claim_ambiguous",
        extra={"attempt_id": str(existing.id), "contact_id": str(contact.id)},
    )
    resolved, reconcile_note = _reconcile_with_dograh(db, existing)
    if resolved is not None:
        return resolved
    _record_dograh_trigger_failure(
        db,
        existing,
        contact,
        event_type="DOGRAH_TRIGGER_AMBIGUOUS",
        status_code=None,
        category="orphaned_claim",
        reconcile=reconcile_note,
    )
    return JobOutcome.AMBIGUOUS_RECOVERY


def _record_dograh_trigger_failure(
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    *,
    event_type: str,
    status_code: int | None,
    category: str,
    reconcile: str | None = None,
) -> None:
    """Terminal never-connected outcome for a trigger that did not yield a run;
    the ONLY follow-up is RecoveryManager's decision (backoff, bounds,
    suppression/eligibility re-checks)."""
    attempt.provider = "dograh"
    attempt.state = CallAttemptState.FAILED_TO_CONNECT
    attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
    attempt.ended_at = datetime.now(UTC)
    payload: dict = {"status_code": status_code, "category": category}
    if reconcile is not None:
        payload["reconcile"] = reconcile
    db.add(CallEvent(call_attempt_id=attempt.id, event_type=event_type, payload=payload))
    db.flush()
    _handle_never_connected_failure(
        db, attempt, contact, NeverConnectedFailureReason.PROVIDER_ERROR
    )


def _reconcile_with_dograh(db: Session, attempt: CallAttempt) -> tuple[str | None, str]:
    """Ask Dograh whether an ambiguous trigger already created a run for this
    attempt (documented run listing; see DograhClient.find_runs_for_attempt).

    Returns (outcome, note). outcome is None when nothing was resolved and the
    caller must fall back to the normal ambiguous path -- note says why
    ("no_run_found" / "unavailable"). "no_run_found" is NOT proof that no call
    exists (a timed-out request can still land later), so it never relaxes the
    RecoveryManager backoff.
    """
    from app.services.telephony.dograh_client import DograhApiError, DograhConfigurationError
    from app.services.telephony.factory import get_dograh_client

    try:
        run_ids = get_dograh_client().find_runs_for_attempt(str(attempt.id), attempt.started_at)
    except (DograhApiError, DograhConfigurationError) as exc:
        logger.warning(
            "dograh_reconcile_unavailable",
            extra={"attempt_id": str(attempt.id), "error_type": type(exc).__name__},
        )
        return None, "unavailable"

    if not run_ids:
        return None, "no_run_found"

    if len(run_ids) > 1:
        # Never silently choose. The duplicate already exists, so retrying
        # would only add a third call; the webhooks (keyed by call_attempt_id)
        # will resolve the attempt, and an operator is told.
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type="DOGRAH_RECONCILE_MULTIPLE",
                payload={"workflow_run_ids": run_ids},
            )
        )
        record_audit_event(
            db,
            actor="dialer-worker",
            action="dograh.reconcile_multiple_candidates",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"workflow_run_ids": run_ids},
        )
        db.flush()
        logger.error(
            "dograh_reconcile_multiple_candidates",
            extra={"attempt_id": str(attempt.id), "count": len(run_ids)},
        )
        return JobOutcome.RECONCILE_MULTIPLE, "multiple"

    # Exactly one: adopt it. State stays INITIATED / contact stays DIALING --
    # the completion webhook decides what actually happened.
    attempt.provider = "dograh"
    attempt.provider_call_id = str(run_ids[0])
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type="DOGRAH_TRIGGER_RECONCILED",
            payload={"workflow_run_id": run_ids[0]},
        )
    )
    db.flush()
    logger.info(
        "dograh_trigger_reconciled",
        extra={"attempt_id": str(attempt.id), "workflow_run_id": run_ids[0]},
    )
    return JobOutcome.RECONCILED, "adopted"


def _place_call(
    db: Session,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    contact: Contact,
    attempt: CallAttempt,
    job: DialJob,
) -> str | None:
    from app.core.config import get_settings

    if get_settings().calling_engine == "dograh":
        # Checkpoint 08: telephony + STT + LLM + TTS all happen inside
        # Dograh's own pipeline once triggered -- there is no live
        # audio connection for our own ConversationOrchestrator to
        # drive, so _start_conversation_safely is deliberately never
        # called on this path. The call's outcome arrives later, once,
        # via app/api/routes/dograh_webhook.py.
        return _place_call_via_dograh(db, contact, attempt, job)

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
    return None


def _place_call_via_dograh(
    db: Session, contact: Contact, attempt: CallAttempt, job: DialJob
) -> str | None:
    """Checkpoint 08/09: one HTTP call replaces both `create_outbound_call`
    and the conversation-start step above -- Dograh runs the entire
    call (dial, STT, LLM, TTS) autonomously once triggered. Our
    `call_attempt_id` is round-tripped through Dograh's
    `initial_context`, which Dograh echoes back in its webhook payload
    (see app/api/routes/dograh_webhook.py) -- that's how the webhook
    correlates back to this specific CallAttempt with no shared
    database between the two systems.

    Checkpoint 09 §2: a successful trigger response means Dograh
    *accepted the job*, not that a phone connected -- `attempt.state`
    deliberately stays at its default INITIATED and `contact.status`
    stays DIALING (set by `_dial` just before this call). The only
    lifecycle signal this integration ever receives is the single
    completion webhook, which is what actually determines whether the
    call connected at all (see dograh_webhook_service.py's three-way
    classification).
    """
    from app.core.redis_client import get_redis
    from app.services.telephony.dograh_client import (
        AMBIGUOUS_CATEGORIES,
        DograhApiError,
        DograhConfigurationError,
    )
    from app.services.telephony.factory import get_dograh_client

    campaign = db.get(Campaign, contact.campaign_id)
    initial_context = {
        "call_attempt_id": str(attempt.id),
        "contact_id": str(contact.id),
        "campaign_id": str(contact.campaign_id),
        "campaign_name": campaign.name if campaign is not None else "",
    }
    log_extra = {
        "attempt_id": str(attempt.id),
        "contact_id": str(contact.id),
        "campaign_id": str(contact.campaign_id),
        "trace_id": job.trace_id,
    }
    dograh_breaker = CircuitBreaker(get_redis(), "dograh")

    # Durable claim BEFORE the non-idempotent trigger: if this process dies
    # anywhere after the request leaves, the attempt row survives, so a
    # reclaimed job sees it and recovers instead of dialing a second time.
    db.commit()

    try:
        client = get_dograh_client()
        result = client.trigger_call(
            phone_number=contact.normalized_phone_number, initial_context=initial_context
        )
    except DograhConfigurationError:
        logger.exception("dograh_not_configured", extra=log_extra)
        attempt.provider = "dograh"
        attempt.state = CallAttemptState.FAILED_TO_CONNECT
        attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
        attempt.ended_at = datetime.now(UTC)
        db.flush()
        return None
    except DograhApiError as exc:
        is_ambiguous = exc.category in AMBIGUOUS_CATEGORIES
        logger.warning(
            "dograh_trigger_failed",
            extra={
                **log_extra,
                "status_code": exc.status_code,
                "category": exc.category.value,
                "ambiguous": is_ambiguous,
            },
        )
        # Checkpoint 09 §1.3: an ambiguous outcome (the request may have
        # reached Dograh before the response was lost) is never retried
        # immediately. First ask Dograh's documented run listing whether a run
        # already exists for this attempt: exactly one => adopt it (no second
        # call); several => never choose; none/unavailable => fall through to
        # the normal never-connected path, i.e. RecoveryManager's 30s/10min
        # backoff and re-checks. "None found" is not proof of absence, so it
        # never shortens that backoff.
        reconcile_note: str | None = None
        if is_ambiguous:
            resolved, reconcile_note = _reconcile_with_dograh(db, attempt)
            if resolved is not None:
                if resolved == JobOutcome.RECONCILED:
                    dograh_breaker.record_success()  # Dograh did accept the trigger
                else:
                    dograh_breaker.record_failure()
                return resolved
        dograh_breaker.record_failure()
        _record_dograh_trigger_failure(
            db,
            attempt,
            contact,
            event_type="DOGRAH_TRIGGER_AMBIGUOUS" if is_ambiguous else "DOGRAH_TRIGGER_FAILED",
            status_code=exc.status_code,
            category=exc.category.value,
            reconcile=reconcile_note,
        )
        return None

    dograh_breaker.record_success()
    attempt.provider = "dograh"
    attempt.provider_call_id = str(result.workflow_run_id)
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type="DOGRAH_CALL_TRIGGERED",
            payload={"workflow_run_id": result.workflow_run_id},
        )
    )
    db.flush()
    logger.info(
        "dograh_call_triggered", extra={**log_extra, "workflow_run_id": result.workflow_run_id}
    )
    return None


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
