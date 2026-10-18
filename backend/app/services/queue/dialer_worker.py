"""The calling worker -- Checkpoint 03 Steps 5, 15-19, 22.

Processing contract for one job:

  read -> admission (rate/concurrency/circuit) -> load DB state ->
  final eligibility -> claim/create CallAttempt -> dial via provider ->
  persist outcome -> release concurrency lease -> ack

Nothing is acked before the outcome is durably persisted (Step 5). A
job that fails admission is left unacked so it can be retried by this
or another worker shortly (backpressure -- Step 10) rather than being
dropped.
"""

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    NeverConnectedFailureReason,
)
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository

# kill_switch stays importable from here: tests and ops tooling patch it via this module.
from app.services import kill_switch, outbound_gate, provider_halt  # noqa: F401
from app.services.audit_service import record_audit_event
from app.services.calling_window import InvalidTimezoneError, NoDialableWindowError
from app.services.eligibility_service import DialEligibilityService
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.deadline import JobDeadlineExceeded, job_deadline
from app.services.queue.enqueue_service import enqueue_guard_key
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import DeadLetter, RedisStreamQueue
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.job import RecoveryJob
from app.services.recovery.manager import RecoveryManager
from app.services.retry_policy_service import (
    get_effective_policy,
    next_open_time,
    window_is_open,
)
from app.services.telephony.base import ProviderOutcome, TelephonyProvider
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_outcome import (
    REASON_CONFIGURATION,
    REASON_PROVIDER_ERROR,
    TriggerFailure,
    TriggerPolicy,
    classify_trigger_error,
    provider_configuration_category,
)
from app.services.telephony.dograh_reconciliation import (
    AMBIGUOUS_TRIGGER_EVENT,
    is_unresolved_ambiguous_trigger,
    reopen_for_adoption,
)

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
    # CP11: global kill switch is on (or unreadable -> fail closed). Left UNACKED so
    # the job stays queued and is re-driven once outbound is re-enabled.
    OUTBOUND_BLOCKED = "outbound_blocked"
    # HF1 1.5: the provider rejected our CONFIGURATION (credential / trigger / quota), or the
    # systemic detector tripped. The contact is restored (dialable, retry budget untouched), the
    # attempt is recorded with a sanitized category, `provider_halt` is engaged, and the job is
    # left UNACKED so it is re-driven once an admin releases the halt.
    PROVIDER_HALTED = "provider_halted"
    # The pre-retry lookup could not establish whether the earlier ambiguous
    # trigger created a call. Fail closed: no dial, left UNACKED so a
    # redelivery re-checks instead of dialing blind.
    RECONCILE_DEFERRED = "reconcile_deferred"
    # CP13: the lookup kept failing past DOGRAH_RECONCILE_MAX_DEFERRALS. Whether the earlier
    # trigger created a call can no longer be established, so the retry is abandoned (acked,
    # no dial) instead of polled forever. Duplicate-call avoidance wins over the retry.
    RECONCILE_EXPIRED = "reconcile_expired"
    # CP12-B: the campaign is paused. The job is NOT lost, NOT counted as handled, and no
    # concurrency lease / CPS slot was (or stays) taken. Two dispositions, see _settle_paused:
    #   CAMPAIGN_PAUSED       -- first-attempt job whose logical record is still the Postgres
    #                            contact row: its Redis reference (and enqueue guard) is dropped
    #                            (acked) and resume re-queues it once.
    #   CAMPAIGN_PAUSED_HELD  -- anything else (retries, recovery jobs, attempt claims): left
    #                            UNACKED in the pending list, re-driven by the normal reclaim.
    CAMPAIGN_PAUSED = "campaign_paused"
    CAMPAIGN_PAUSED_HELD = "campaign_paused_held"
    # CP14 (C5a): the campaign's calling window is closed. "Not now", never "not eligible"
    # (NOT_ELIGIBLE is acked and used to drop the job). OUTSIDE_WINDOW is what _dial reports;
    # the settle step turns it into one of the two dispositions:
    #   WINDOW_DEFERRED -- the job now lives in the recovery scheduler, due at the next
    #                      opening (and, for a first attempt, PostgreSQL can still rebuild
    #                      it); the stream message is acked.
    #   WINDOW_HELD     -- anything that cannot be moved safely: left UNACKED in the
    #                      pending list, re-driven by the normal reclaim.
    OUTSIDE_WINDOW = "outside_window"
    WINDOW_DEFERRED = "window_deferred"
    WINDOW_HELD = "window_held"
    # CP14: the daily dial/spend budget is exhausted or unreadable (fail closed). Left
    # UNACKED, no admission slot taken, no attempt claimed, no retry budget consumed.
    BUDGET_BLOCKED = "budget_blocked"
    NO_JOB = "no_job"
    # P1-6: the job ran past JOB_DEADLINE_SECONDS and was abandoned. Left UNACKED and deliberately
    # NOT a hold: it keeps counting deliveries, so a job that always hangs ends in the dead-letter
    # stream instead of occupying the worker for ever.
    DEADLINE_EXCEEDED = "deadline_exceeded"


def _utcnow() -> datetime:
    """The dialer's clock for window and budget decisions (tests inject a fake one)."""
    return datetime.now(UTC)


def _gate_outcome(db: Session, reason: str) -> str:
    """Map an outbound-gate reason to its job outcome (both are left UNACKED)."""
    if outbound_gate.is_budget_reason(reason):
        if reason == "budget_unavailable":
            db.rollback()  # a failed read leaves the session unusable until rolled back
        return JobOutcome.BUDGET_BLOCKED
    return JobOutcome.OUTBOUND_BLOCKED


_BLOCKED_POLL_LOG_INTERVAL_SECONDS = 60.0
_last_blocked_poll_log = 0.0


def _log_blocked_poll(reason: str) -> None:
    """One warning per minute per process while outbound is blocked (not one per poll)."""
    global _last_blocked_poll_log
    now = time.monotonic()
    if now - _last_blocked_poll_log >= _BLOCKED_POLL_LOG_INTERVAL_SECONDS:
        _last_blocked_poll_log = now
        logger.warning("outbound_blocked_not_reading_queue", extra={"reason": reason})


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
    # CP11 kill switch (CP14: and the daily budget), stage 0: do not even READ a job while
    # outbound is blocked.
    # A job that is read but not processed sits in the consumer group's pending list, and
    # that list is only re-driven 10 jobs per ~50 loop iterations (queue.reclaim_stale).
    # Pulling a whole backlog into it during a freeze would turn "resume" into a crawl.
    # Unread jobs stay in the stream and are consumed at full speed once it is off.
    # Stages 1 and 2 below remain the safety net for a switch flipped after the read.
    if (reason := outbound_gate.block_reason(db, _utcnow())) is not None:
        _log_blocked_poll(reason)
        time.sleep(block_ms / 1000)  # same pacing as an empty blocking read: no hot loop
        return _gate_outcome(db, reason)

    read = queue.read_one(consumer_name, block_ms)
    handle_dead_letters(db, queue)  # anything the read moved to the DLQ (unreadable payloads)
    if read is None:
        return JobOutcome.NO_JOB

    message_id, job = read
    return process_claimed_job(db, queue, admission, provider, circuit_breaker, message_id, job)


DLQ_ACTOR = "dead-letter-handler"
DLQ_AUDIT_ACTION = "queue.dead_lettered"
# Only a contact that has not started dialing is parked: a DIALING contact has an attempt in
# flight, which the stuck-call sweeper owns.
_PARKABLE = (ContactStatus.PENDING, ContactStatus.RETRY_SCHEDULED)


def handle_dead_letters(db: Session, queue: RedisStreamQueue) -> int:
    """Record, in PostgreSQL, every message the queue just moved to the dead-letter stream:
    an audit event for each, and its contact set to NEEDS_REVIEW (never dialed automatically,
    never re-enqueued) unless a call is already in flight. The DLQ stream is the durable record;
    a failure here is logged and never raised, so it cannot block the loop."""
    letters = queue.drain_dead_letters()
    for letter in letters:
        try:
            _record_dead_letter(db, letter)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("dead_letter_record_failed", extra={"message_id": letter.message_id})
    return len(letters)


def _record_dead_letter(db: Session, letter: DeadLetter) -> None:
    metadata: dict[str, object] = {
        "reason": letter.reason,
        "message_id": letter.message_id,
        "times_delivered": letter.times_delivered,
        "dlq_stream": queue_dlq_key(),
    }
    entity_type, entity_id = "queue_message", None
    if letter.job is not None:
        contact = db.get(Contact, uuid.UUID(letter.job.contact_id))
        metadata["attempt_number"] = letter.job.attempt_number
        if contact is not None:
            entity_type, entity_id = "contact", contact.id
            metadata["previous_status"] = contact.status.value
            if contact.status in _PARKABLE:
                contact.status = ContactStatus.NEEDS_REVIEW
                # A retry that can never run must not be rebuilt into the schedule (P1-2).
                contact.next_retry_at = None
                contact.retry_attempt_id = None
                contact.retry_attempt_number = None
    record_audit_event(
        db,
        actor=DLQ_ACTOR,
        action=DLQ_AUDIT_ACTION,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata,
    )


def queue_dlq_key() -> str:
    return f"{get_settings().queue_stream_key}:dlq"


def reclaim_and_redrive(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    consumer_name: str,
) -> int:
    """Claim jobs another consumer left pending for `queue_reclaim_idle_ms` and drive each one
    through the normal claim -> validate -> execute -> persist -> ack contract.

    Each job is isolated: an exception in one (a poison job) is logged, its delivery count keeps
    growing toward the dead-letter threshold, and the rest of the batch still runs. Returns the
    number of jobs re-driven successfully."""
    reclaimed = queue.reclaim_stale(consumer_name, get_settings().queue_reclaim_idle_ms)
    handle_dead_letters(db, queue)
    done = 0
    for message_id, job in reclaimed:
        try:
            outcome = process_claimed_job(
                db, queue, admission, provider, circuit_breaker, message_id, job
            )
            db.commit()
            done += 1
            logger.info("reclaimed_job_processed", extra={"outcome": outcome})
        except Exception:
            db.rollback()
            logger.exception("reclaimed_job_failed", extra={"message_id": message_id})
    return done


_LEFT_UNACKED = frozenset(
    {
        JobOutcome.IN_FLIGHT,
        JobOutcome.RECONCILE_DEFERRED,
        JobOutcome.OUTBOUND_BLOCKED,
        JobOutcome.PROVIDER_HALTED,
        JobOutcome.BUDGET_BLOCKED,
        JobOutcome.CAMPAIGN_PAUSED_HELD,
        JobOutcome.WINDOW_HELD,
    }
)


def _campaign_is_paused(db: Session, campaign_id: str) -> bool:
    """The durable campaign state, read fresh from PostgreSQL on every job (CP12-B).

    A column query, deliberately not `db.get(Campaign, ...)`: the identity map could hand back
    a copy loaded before the operator paused the campaign. Redis never decides this -- a stale
    or replayed queue entry cannot out-vote the database."""
    status = db.execute(
        select(Campaign.status).where(Campaign.id == uuid.UUID(campaign_id))
    ).scalar_one_or_none()
    return status == CampaignStatus.PAUSED


def _settle_paused(db: Session, queue: RedisStreamQueue, message_id: str, job: DialJob) -> str:
    """Decide what happens to a job read for a paused campaign. Nothing is dialed and no
    attempt row, attempt count or retry state is touched, so a paused job can never turn into
    a failure, a retry or a duplicate. Never sleeps or re-queues: pausing costs one Redis
    delete + ack per consumed message, and nothing at all for messages that are never read.
    """
    contact = db.execute(
        select(Contact.status, Contact.attempt_count).where(
            Contact.id == uuid.UUID(job.contact_id)
        )
    ).one_or_none()
    already_dialed = db.execute(
        select(CallAttempt.provider_call_id).where(
            CallAttempt.contact_id == uuid.UUID(job.contact_id),
            CallAttempt.attempt_number == job.attempt_number,
        )
    ).scalar_one_or_none()

    # Rebuildable == exactly what QueueEnqueueService selects: PENDING and never attempted.
    rebuildable = (
        job.attempt_number == 1
        and job.recovery_type is None
        and contact is not None
        and contact.status == ContactStatus.PENDING
        and contact.attempt_count == 0
    )
    if rebuildable:
        # Guard first, ack second: a crash in between leaves the message pending (re-driven,
        # worst case a harmless duplicate reference) rather than a job nobody can rebuild.
        queue.redis.delete(enqueue_guard_key(job.idempotency_key))
        queue.ack(message_id)
        outcome = JobOutcome.CAMPAIGN_PAUSED
    elif already_dialed is not None:
        queue.ack(message_id)  # duplicate delivery of a call that was already placed
        outcome = JobOutcome.ALREADY_PROCESSED
    else:
        outcome = JobOutcome.CAMPAIGN_PAUSED_HELD
    logger.info(
        "campaign_job_blocked_paused",
        extra={
            "job_id": job.job_id,
            "trace_id": job.trace_id,
            "campaign_id": job.campaign_id,
            "disposition": outcome,
        },
    )
    return outcome


def _closed_window(
    db: Session, job: DialJob, now: datetime
) -> tuple[bool, datetime | None]:
    """(closed, next_opening). `closed` is True when the campaign's window is shut at `now`
    -- or cannot be evaluated at all (corrupt timezone, window that never overlaps the hard
    bound), in which case next_opening is None: never dial on a guess, never drop the job."""
    campaign = db.get(Campaign, uuid.UUID(job.campaign_id))
    if campaign is None or campaign.status != CampaignStatus.ACTIVE:
        return False, None  # not a window question; _dial resolves it
    policy = get_effective_policy(db, campaign)
    try:
        if window_is_open(campaign, policy, now):
            return False, None
        return True, next_open_time(campaign, policy, now)
    except (InvalidTimezoneError, NoDialableWindowError):
        logger.error("calling_window_unresolvable", extra={"campaign_id": job.campaign_id})
        return True, None


def _settle_outside_window(
    db: Session,
    queue: RedisStreamQueue,
    message_id: str,
    job: DialJob,
    due_at: datetime | None,
) -> str:
    """What happens to a job read while its calling window is closed. Nothing is dialed and
    no attempt row, attempt count or retry state is touched. The order is the CP12-B one --
    make the job exist in its new home FIRST, release the old one SECOND -- so a crash at any
    point leaves it in at least one place (the stream message stays pending until acked)."""
    from app.services.recovery.factory import get_recovery_scheduler

    contact = db.execute(
        select(Contact.status, Contact.attempt_count).where(
            Contact.id == uuid.UUID(job.contact_id)
        )
    ).one_or_none()
    already_dialed = db.execute(
        select(CallAttempt.provider_call_id).where(
            CallAttempt.contact_id == uuid.UUID(job.contact_id),
            CallAttempt.attempt_number == job.attempt_number,
        )
    ).scalar_one_or_none()
    rebuildable = (
        job.attempt_number == 1
        and job.recovery_type is None
        and contact is not None
        and contact.status == ContactStatus.PENDING
        and contact.attempt_count == 0
    )

    if rebuildable and due_at is not None:
        get_recovery_scheduler().schedule_dial(job, due_at)  # 1. it exists in the scheduler
        queue.redis.delete(enqueue_guard_key(job.idempotency_key))  # 2. Postgres can rebuild it
        queue.ack(message_id)  # 3. only now is the stream reference released
        outcome = JobOutcome.WINDOW_DEFERRED
    elif already_dialed is not None:
        queue.ack(message_id)  # duplicate delivery of a call that was already placed
        outcome = JobOutcome.ALREADY_PROCESSED
    elif job.recovery_type is not None and job.previous_attempt_id and due_at is not None:
        # A retry that arrived just after the window closed goes back to the scheduler, the
        # same home it came from.
        get_recovery_scheduler().schedule(
            RecoveryJob(
                job_id=job.job_id,
                attempt_id=job.previous_attempt_id,
                contact_id=job.contact_id,
                campaign_id=job.campaign_id,
                attempt_number=job.attempt_number,
                recovery_type=job.recovery_type,
                idempotency_key=job.idempotency_key,
                scheduled_at=_utcnow().isoformat(),
                trace_id=job.trace_id,
            ),
            due_at,
        )
        queue.ack(message_id)
        outcome = JobOutcome.WINDOW_DEFERRED
    else:
        outcome = JobOutcome.WINDOW_HELD  # left unacked, re-driven by the normal reclaim
    logger.info(
        "job_blocked_outside_window",
        extra={
            "job_id": job.job_id,
            "trace_id": job.trace_id,
            "campaign_id": job.campaign_id,
            "disposition": outcome,
        },
    )
    return outcome


def process_claimed_job(
    db: Session,
    queue: RedisStreamQueue,
    admission: AdmissionController,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    message_id: str,
    job: DialJob,
) -> str:
    """Process one claimed delivery. A job the outcome deliberately leaves unacked is a hold, not a
    failure: its delivery counter is reset so reclaiming it again and again (a campaign paused for
    hours) is never mistaken for a poison message (P1-5). An exception skips the reset, so a job
    that keeps raising keeps counting up and ends in the dead-letter stream."""
    try:
        with job_deadline(get_settings().job_deadline_seconds):
            outcome = _process_claimed_job(
                db, queue, admission, provider, circuit_breaker, message_id, job
            )
    except JobDeadlineExceeded:
        # Abandoned. Whatever was already committed (the attempt claim) stays; the rest is
        # discarded. Same state as a worker killed here: reclaim re-drives the unacked entry and
        # an existing attempt is reconciled, never triggered a second time.
        db.rollback()
        logger.error("job_deadline_exceeded", extra={"message_id": message_id})
        return JobOutcome.DEADLINE_EXCEEDED
    if outcome in _LEFT_UNACKED:
        queue.reset_deliveries(message_id)
    return outcome


def _process_claimed_job(
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

    # CP11 kill switch, stage 1: checked before admission so a blocked job never
    # consumes a concurrency/CPS slot. Stage 2 (the one that matters) is in _dial.
    if (reason := outbound_gate.block_reason(db, _utcnow())) is not None:
        logger.warning(
            "outbound_blocked",
            extra={"job_id": job.job_id, "trace_id": job.trace_id, "reason": reason},
        )
        return _gate_outcome(db, reason)  # left unacked on purpose: job stays queued

    # CP12-B: durable campaign pause, checked before admission so a paused job never takes a
    # concurrency lease or CPS slot. The same check inside _dial covers a pause that lands
    # after this read (that path releases the lease it already holds).
    if _campaign_is_paused(db, job.campaign_id):
        return _settle_paused(db, queue, message_id, job)

    # CP14: a closed calling window is also settled BEFORE admission, otherwise a backlog
    # read after the window closed would spend CPS/concurrency slots just to be deferred.
    # The authoritative check is still the final eligibility inside _dial.
    closed, due_at = _closed_window(db, job, _utcnow())
    if closed:
        return _settle_outside_window(db, queue, message_id, job, due_at)

    # CP12-A: the concurrency slot is a lease keyed by the job's idempotency key (one logical
    # attempt = one slot; the CallAttempt row does not exist yet at admission). It is released
    # in the `finally` below, and expires on its own if this process dies before reaching it.
    lease = None
    for _ in range(_ADMISSION_RETRY_ATTEMPTS):
        result = admission.try_admit(
            campaign_id=job.campaign_id,
            provider_name=provider_name,
            holder_id=job.idempotency_key,
        )
        if result.admitted:
            lease = result.lease
            break
        time.sleep(_ADMISSION_RETRY_SLEEP_SECONDS)

    if lease is None:
        logger.info(
            "admission_backpressure",
            extra={"job_id": job.job_id, "trace_id": job.trace_id, "reason": result.reason},
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
        admission.release(lease)
    if outcome == JobOutcome.CAMPAIGN_PAUSED:  # paused after the early check; lease released
        return _settle_paused(db, queue, message_id, job)
    if outcome == JobOutcome.OUTSIDE_WINDOW:  # the window closed after the early check
        _, due_at = _closed_window(db, job, _utcnow())
        return _settle_outside_window(db, queue, message_id, job, due_at)
    if outcome not in _LEFT_UNACKED:
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
    # HF1 1.5: an attempt that the provider-configuration hold recorded but never triggered is
    # NOT an unresolved claim -- no call exists. It is reused (below, after the gate) so a job
    # re-driven after the halt is released, or rebuilt from PostgreSQL after a Redis loss, dials
    # the contact exactly once on the same attempt row.
    held = (
        existing
        if existing is not None and is_dograh and _is_held_attempt(db, existing)
        else None
    )
    if existing is not None and is_dograh and held is None:
        return _resolve_unresolved_dograh_claim(db, existing, contact, job)

    # CP12-B: a paused campaign is "not now", never "not eligible" -- NOT_ELIGIBLE is acked,
    # which used to drop the job. Checked after the idempotency guard above so an already
    # dialed duplicate still resolves as ALREADY_PROCESSED.
    if campaign.status == CampaignStatus.PAUSED:
        return JobOutcome.CAMPAIGN_PAUSED

    eligibility = DialEligibilityService(suppressions).check(
        contact, campaign, get_effective_policy(db, campaign), now=_utcnow()
    )
    if not eligibility.eligible:
        if eligibility.transient:
            return JobOutcome.OUTSIDE_WINDOW  # "not now": settled, never acked-and-lost
        logger.info(
            "dial_not_eligible",
            extra={"job_id": job.job_id, "reason": eligibility.reason},
        )
        return JobOutcome.NOT_ELIGIBLE

    # Retry safety gate: RecoveryManager has decided this retry is due; before
    # it is actually dialed, re-check whether the earlier ambiguous trigger
    # in fact created a call. Eligibility has already passed (suppression,
    # campaign status, calling window), so this can never bypass it.
    if is_dograh and job.attempt_number > 1:
        blocked = _second_reconciliation_gate(db, attempts, contact, job)
        if blocked is not None:
            return blocked

    # CP11 kill switch, stage 2 -- the final admission check. Everything that can
    # place a call (first dial, queued backlog, retries and recovery re-enqueues all
    # arrive as DialJobs) passes through here, and it sits BEFORE the durable attempt
    # claim so a blocked job leaves no half-claimed attempt for reconciliation to
    # misread. Residual race: a switch flipped after this read but before the HTTP
    # trigger below still lets that one in-flight trigger through -- the check and the
    # external call cannot be atomic. The window is one DB commit plus one HTTP request.
    if (reason := outbound_gate.block_reason(db, _utcnow())) is not None:
        logger.warning(
            "outbound_blocked",
            extra={"job_id": job.job_id, "trace_id": job.trace_id, "reason": reason},
        )
        # CP14: the budget is part of this same final gate (and fails closed). Both leave
        # the job unacked with no attempt claimed, so no retry budget is consumed.
        return JobOutcome.BUDGET_BLOCKED if outbound_gate.is_budget_reason(
            reason
        ) else JobOutcome.OUTBOUND_BLOCKED

    # What to restore if the provider-configuration hold has to undo this claim (HF1 1.5).
    prior = _Prior(status=contact.status, attempt_count=contact.attempt_count)

    if held is not None:
        attempt, created = held, True  # re-claimed: counts as an attempt again, row reused
        _resume_held_attempt(db, held)
    else:
        attempt, created = attempts.get_or_create(contact.id, job.attempt_number)

        if not created and (attempt.provider_call_id is not None or is_dograh):
            # Lost a race with another delivery/worker between the
            # idempotency check above and claiming here.
            logger.info("duplicate_delivery_ignored", extra={"job_id": job.job_id})
            return JobOutcome.ALREADY_PROCESSED

    if created:
        contact.attempt_count += 1
    contact.status = ContactStatus.DIALING
    # P1-2: the retry (if this was one) is now claimed; nothing is left to rebuild or re-dispatch.
    contact.next_retry_at = None
    contact.retry_attempt_id = None
    contact.retry_attempt_number = None
    db.flush()

    outcome = _place_call(db, provider, circuit_breaker, contact, attempt, job, prior=prior)
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
    resolved, reconcile_note = _reconcile_with_dograh(db, existing, contact, job)
    if resolved is not None:
        return resolved
    _record_dograh_trigger_failure(
        db,
        existing,
        contact,
        event_type=AMBIGUOUS_TRIGGER_EVENT,
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
    reason_key: str = REASON_PROVIDER_ERROR,
    retry_after_seconds: int | None = None,
) -> None:
    """Terminal never-connected outcome for a trigger that did not yield a run;
    the ONLY follow-up is RecoveryManager's decision (backoff, bounds,
    suppression/eligibility re-checks). `reason_key` (CP13) tells it what kind of failure
    this was; this function never decides retry vs terminal itself."""
    attempt.provider = "dograh"
    attempt.state = CallAttemptState.FAILED_TO_CONNECT
    attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
    attempt.ended_at = datetime.now(UTC)
    payload: dict = {"status_code": status_code, "category": category, "reason_key": reason_key}
    if reconcile is not None:
        payload["reconcile"] = reconcile
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    db.add(CallEvent(call_attempt_id=attempt.id, event_type=event_type, payload=payload))
    record_audit_event(
        db,
        actor="dialer-worker",
        action=f"dograh.{reason_key}",
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={
            "campaign_id": str(contact.campaign_id),
            "status_code": status_code,
            "category": category,
            "retry_after_seconds": retry_after_seconds,
        },
    )
    db.flush()
    _handle_never_connected_failure(
        db,
        attempt,
        contact,
        NeverConnectedFailureReason.PROVIDER_ERROR,
        reason_key=reason_key,
        retry_after_seconds=retry_after_seconds,
    )


def _reconcile_ids(contact: Contact, attempt: CallAttempt, job: DialJob) -> dict[str, str]:
    """Safe identifiers only -- never a phone number, secret or transcript."""
    return {
        "campaign_id": str(contact.campaign_id),
        "contact_id": str(contact.id),
        "call_attempt_id": str(attempt.id),
        "job_id": job.job_id,
    }


def _lookup_dograh_runs(
    db: Session, attempt: CallAttempt, *, stage: str, ids: dict[str, str]
) -> tuple[list[int] | None, str | None]:
    """One bounded call to Dograh's documented run listing. Returns
    (run_ids, None), or (None, "timeout" | "error") when the answer cannot be
    trusted -- a failed lookup is never converted into "no call exists"."""
    from app.services.telephony.dograh_client import (
        DograhApiError,
        DograhConfigurationError,
        DograhErrorCategory,
    )
    from app.services.telephony.factory import get_dograh_client

    logger.info(f"reconciliation_{stage}_lookup", extra=ids)
    try:
        return get_dograh_client().find_runs_for_attempt(str(attempt.id), attempt.started_at), None
    except (DograhApiError, DograhConfigurationError) as exc:
        timed_out = isinstance(exc, DograhApiError) and exc.category in (
            DograhErrorCategory.TIMEOUT,
            DograhErrorCategory.AMBIGUOUS_REQUEST,
        )
        kind = "timeout" if timed_out else "error"
        logger.warning(
            f"reconciliation_lookup_{kind}",
            extra={**ids, "stage": stage, "error_type": type(exc).__name__},
        )
        record_audit_event(
            db,
            actor="dialer-worker",
            action=f"dograh.reconciliation_lookup_{kind}",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={**ids, "stage": stage, "error_type": type(exc).__name__},
        )
        return None, kind


def _resolve_found_runs(
    db: Session, attempt: CallAttempt, run_ids: list[int], *, stage: str, ids: dict[str, str]
) -> str:
    """Shared by both lookups. Exactly one run: adopt it. Several: never
    choose one, never retry (a duplicate already exists, so another trigger
    would only add a third call)."""
    if len(run_ids) > 1:
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type="DOGRAH_RECONCILE_MULTIPLE",
                payload={"workflow_run_ids": run_ids, "stage": stage},
            )
        )
        record_audit_event(
            db,
            actor="dialer-worker",
            action="dograh.reconciliation_multiple_runs",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={**ids, "stage": stage, "workflow_run_ids": run_ids},
        )
        db.flush()
        logger.error("reconciliation_multiple_runs", extra={**ids, "count": len(run_ids)})
        logger.error("reconciliation_retry_blocked", extra={**ids, "reason": "multiple_runs"})
        return JobOutcome.RECONCILE_MULTIPLE

    # State stays INITIATED / contact DIALING: the completion webhook, not
    # Dograh having accepted a trigger, decides what actually happened.
    attempt.provider = "dograh"
    attempt.provider_call_id = str(run_ids[0])
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type="DOGRAH_TRIGGER_RECONCILED",
            payload={"workflow_run_id": run_ids[0], "stage": stage},
        )
    )
    record_audit_event(
        db,
        actor="dialer-worker",
        action="dograh.reconciliation_run_adopted",
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={**ids, "stage": stage, "workflow_run_id": run_ids[0]},
    )
    db.flush()
    logger.info("reconciliation_run_adopted", extra={**ids, "workflow_run_id": run_ids[0]})
    return JobOutcome.RECONCILED


def _reconcile_with_dograh(
    db: Session, attempt: CallAttempt, contact: Contact, job: DialJob
) -> tuple[str | None, str]:
    """FIRST lookup, straight after an ambiguous trigger (or an orphaned
    claim). Returns (outcome, note); outcome is None when nothing was resolved
    and the caller falls back to the normal never-connected path, i.e.
    RecoveryManager's backoff. "no_run_found" is NOT proof that no call exists
    (a timed-out request can still land later) -- which is exactly why
    _second_reconciliation_gate re-checks just before the retry is dialed."""
    ids = _reconcile_ids(contact, attempt, job)
    run_ids, failure = _lookup_dograh_runs(db, attempt, stage="first", ids=ids)
    if run_ids is None:
        return None, "unavailable"
    if not run_ids:
        logger.info("reconciliation_no_run", extra={**ids, "stage": "first"})
        return None, "no_run_found"
    outcome = _resolve_found_runs(db, attempt, run_ids, stage="first", ids=ids)
    return outcome, "adopted" if outcome == JobOutcome.RECONCILED else "multiple"


def _defer_or_expire_reconciliation(
    db: Session, previous: CallAttempt, contact: Contact, ids: dict[str, str], failure: str | None
) -> str:
    """CP13: the pre-retry lookup failed. Defer (fail closed, no dial) -- but only a bounded
    number of times. Each deferral is a durable audit row on the previous attempt, so the
    count survives a worker restart. Once DOGRAH_RECONCILE_MAX_DEFERRALS deferrals are spent
    the retry is ABANDONED rather than polled forever: whether the earlier trigger created a
    call can no longer be established, and a missed retry is the cheaper error than ringing
    a customer twice."""
    from app.core.config import get_settings
    from app.models.audit_log import AuditLog

    deferred = db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity_type == "call_attempt",
            AuditLog.entity_id == previous.id,
            AuditLog.action == "dograh.reconciliation_deferred",
        )
    ).scalar_one()

    if deferred >= get_settings().dograh_reconcile_max_deferrals:
        logger.warning("reconciliation_expired", extra={**ids, "deferrals": deferred})
        record_audit_event(
            db,
            actor="dialer-worker",
            action="dograh.reconciliation_expired",
            entity_type="call_attempt",
            entity_id=previous.id,
            metadata={**ids, "deferrals": deferred, "lookup_failure": failure},
        )
        contact.status = ContactStatus.COMPLETED_PARTIAL
        db.flush()
        return JobOutcome.RECONCILE_EXPIRED

    if deferred == 0:
        logger.info("reconciliation_started", extra=ids)
    record_audit_event(
        db,
        actor="dialer-worker",
        action="dograh.reconciliation_deferred",
        entity_type="call_attempt",
        entity_id=previous.id,
        metadata={**ids, "lookup_failure": failure, "deferrals": deferred + 1},
    )
    return JobOutcome.RECONCILE_DEFERRED


def _second_reconciliation_gate(
    db: Session, attempts: CallAttemptRepository, contact: Contact, job: DialJob
) -> str | None:
    """SECOND lookup, immediately before an ambiguous retry is dialed.

    RecoveryManager remains the only owner of the retry decision; this only
    decides whether that already-approved retry is still safe. Returns an
    outcome that BLOCKS the dial, or None to let the retry proceed. It runs
    only when the previous attempt ended as an unresolved ambiguous Dograh
    trigger -- never for normal calls or definite provider failures.

    This narrows the duplicate-call window; it cannot close it: a provider
    request can still complete right after the final lookup.
    """
    previous = attempts.get_by_contact_and_number(contact.id, job.attempt_number - 1)
    if previous is None:
        return None
    ids = _reconcile_ids(contact, previous, job)

    if previous.state == CallAttemptState.INITIATED:
        # The earlier attempt is live again (adopted run, or awaiting its
        # webhook): this retry job is stale and must not dial.
        logger.info("reconciliation_retry_blocked", extra={**ids, "reason": "previous_active"})
        return JobOutcome.ALREADY_PROCESSED

    if previous.provider_call_id is not None or not is_unresolved_ambiguous_trigger(db, previous):
        return None  # a definite outcome: nothing to reconcile

    run_ids, failure = _lookup_dograh_runs(db, previous, stage="second", ids=ids)
    if run_ids is None:
        logger.warning("reconciliation_retry_blocked", extra={**ids, "reason": f"lookup_{failure}"})
        return _defer_or_expire_reconciliation(db, previous, contact, ids, failure)

    # Serialise with any other worker holding the same retry. The HTTP lookup
    # is already done, so the row lock is held only for short DB work.
    previous = db.execute(
        select(CallAttempt)
        .where(CallAttempt.id == previous.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if (
        previous.state != CallAttemptState.FAILED_TO_CONNECT
        or previous.provider_call_id is not None
        or attempts.get_by_contact_and_number(contact.id, job.attempt_number) is not None
    ):
        logger.info(
            "reconciliation_retry_blocked", extra={**ids, "reason": "resolved_concurrently"}
        )
        return JobOutcome.ALREADY_PROCESSED

    if run_ids:
        # Reopen narrowly: the webhook refuses terminal attempts, so without
        # this the real call's outcome would be dropped as already_processed.
        reopen_for_adoption(previous, contact)
        return _resolve_found_runs(db, previous, run_ids, stage="second", ids=ids)

    logger.info("reconciliation_no_run", extra={**ids, "stage": "second"})
    record_audit_event(
        db,
        actor="dialer-worker",
        action="dograh.reconciliation_retry_allowed",
        entity_type="call_attempt",
        entity_id=previous.id,
        metadata={**ids, "stage": "second", "next_attempt_number": job.attempt_number},
    )
    logger.info("reconciliation_retry_allowed", extra=ids)
    return None


def _place_call(
    db: Session,
    provider: TelephonyProvider,
    circuit_breaker: CircuitBreaker,
    contact: Contact,
    attempt: CallAttempt,
    job: DialJob,
    prior: "_Prior | None" = None,
) -> str | None:
    from app.core.config import get_settings

    if get_settings().calling_engine == "dograh":
        # Checkpoint 08: telephony + STT + LLM + TTS all happen inside
        # Dograh's own pipeline once triggered -- there is no live
        # audio connection for our own ConversationOrchestrator to
        # drive, so _start_conversation_safely is deliberately never
        # called on this path. The call's outcome arrives later, once,
        # via app/api/routes/dograh_webhook.py.
        return _place_call_via_dograh(db, contact, attempt, job, prior)

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


HELD_EVENT_TYPE = "DOGRAH_PROVIDER_HALT_HELD"
RESUMED_EVENT_TYPE = "DOGRAH_PROVIDER_HALT_RESUMED"


@dataclass(frozen=True)
class _Prior:
    """The contact as it was just BEFORE `_dial` claimed it, so a provider-configuration hold can
    put it back exactly (a first attempt is PENDING/0; a retry is whatever the recovery set)."""

    status: ContactStatus
    attempt_count: int


def _latest_event_type(db: Session, attempt: CallAttempt) -> str | None:
    return db.execute(
        select(CallEvent.event_type)
        .where(CallEvent.call_attempt_id == attempt.id)
        .order_by(CallEvent.occurred_at.desc(), CallEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _is_held_attempt(db: Session, attempt: CallAttempt) -> bool:
    """True only for an attempt the provider-configuration hold recorded and never triggered:
    its LATEST event is the hold marker, it has no provider call and it ended as a failed connect.
    (Any later event -- a resume, a real trigger failure -- makes it an ordinary attempt again.)"""
    return (
        attempt.provider_call_id is None
        and attempt.state == CallAttemptState.FAILED_TO_CONNECT
        and _latest_event_type(db, attempt) == HELD_EVENT_TYPE
    )


def _resume_held_attempt(db: Session, attempt: CallAttempt) -> None:
    attempt.state = CallAttemptState.INITIATED
    attempt.connection_failure_reason = None
    attempt.ended_at = None
    attempt.started_at = datetime.now(UTC)
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type=RESUMED_EVENT_TYPE,
            payload={"note": "provider halt released; re-dialing on the same attempt"},
        )
    )
    db.flush()


def _hold_for_provider_halt(
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    prior: _Prior,
    *,
    category: str,
    status_code: int | None,
    log_extra: Mapping[str, object],
    source_category: str | None = None,
    count: int | None = None,
) -> str:
    """HF1 1.5: the provider's configuration (or the provider as a whole) is the problem, so
    nothing about THIS contact is spent.

    * the attempt is RECORDED with a sanitized category (never raw exception text or a body);
    * the contact goes back exactly where it was and its retry budget is not consumed, so it is
      dialable and rebuildable from PostgreSQL;
    * `provider_halt` is engaged (first error wins; one audit event; no job is acked or lost);
    * the job is left UNACKED (`PROVIDER_HALTED`) and is re-driven after an admin releases.

    If the halt itself cannot be written (database down) the exception propagates: the worker
    rolls back and the job stays pending -- it never carries on dialing without the stop.
    """
    attempt.provider = "dograh"
    attempt.state = CallAttemptState.FAILED_TO_CONNECT
    attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
    attempt.ended_at = datetime.now(UTC)
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type=HELD_EVENT_TYPE,
            payload={"category": category, "status_code": status_code},
        )
    )
    contact.status = prior.status
    contact.attempt_count = prior.attempt_count
    db.flush()

    provider_halt.engage(
        category,
        provider_halt.reason_for(
            category, status_code=status_code, count=count, source_category=source_category
        ),
        metadata={
            "status_code": status_code,
            "campaign_id": log_extra.get("campaign_id"),
            "attempt_id": log_extra.get("attempt_id"),
        },
    )
    logger.error(
        "provider_halt_hold",
        extra={**log_extra, "category": category, "status_code": status_code},
    )
    return JobOutcome.PROVIDER_HALTED


def _trigger_event_type(failure: TriggerFailure) -> str:
    if failure.policy == TriggerPolicy.AMBIGUOUS:
        return AMBIGUOUS_TRIGGER_EVENT
    return "DOGRAH_TRIGGER_FAILED"  # the payload's reason_key says which kind


def _place_call_via_dograh(
    db: Session,
    contact: Contact,
    attempt: CallAttempt,
    job: DialJob,
    prior: "_Prior | None" = None,
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
    from app.core.config import get_settings
    from app.core.redis_client import get_redis
    from app.services.telephony.dograh_client import DograhApiError, DograhConfigurationError
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
        # Raised while building the client, i.e. before any request left this
        # process, so no call can exist. Recorded and audited like every other
        # trigger failure (the contact must not be left stranded in DIALING)
        # and handed to RecoveryManager's bounded policy. Not a provider-health
        # signal, so the circuit breaker is not touched.
        logger.error("dograh_not_configured", extra=log_extra)
        if prior is not None:
            # HF1 1.5: a missing key/trigger is the PROVIDER's configuration, not this contact's
            # problem: do not burn the contact, halt dialing instead.
            return _hold_for_provider_halt(
                db, attempt, contact, prior, category=provider_halt.CONFIGURATION_ERROR,
                status_code=None, log_extra=log_extra,
            )
        _record_dograh_trigger_failure(
            db,
            attempt,
            contact,
            event_type="DOGRAH_CONFIGURATION_ERROR",
            status_code=None,
            category="configuration_error",
            reason_key=REASON_CONFIGURATION,
        )
        return None
    except DograhApiError as exc:
        config_category = provider_configuration_category(exc)
        if config_category is not None and prior is not None:
            # HF1 1.5: 401/403/404/402 -- our credential, trigger or quota. Nothing is wrong with
            # THIS contact; burning it (and the next, and the next) is the defect. Hold + halt.
            logger.error(
                "provider_configuration_error_halting",
                extra={**log_extra, "status_code": exc.status_code, "category": config_category},
            )
            return _hold_for_provider_halt(
                db, attempt, contact, prior, category=config_category,
                status_code=exc.status_code, log_extra=log_extra,
            )
        failure = classify_trigger_error(exc)
        is_ambiguous = failure.policy == TriggerPolicy.AMBIGUOUS
        logger.warning(
            "dograh_trigger_failed",
            extra={
                **log_extra,
                "status_code": exc.status_code,
                "category": exc.category.value,
                "policy": failure.policy.value,
                "reason_key": failure.reason_key,
                "ambiguous": is_ambiguous,
            },
        )
        if failure.policy == TriggerPolicy.RATE_LIMITED:
            logger.warning(
                "provider_rate_limited",
                extra={**log_extra, "retry_after_seconds": failure.retry_after_seconds},
            )
            if failure.retry_after_seconds is not None:
                logger.info(
                    "retry_after_applied",
                    extra={**log_extra, "retry_after_seconds": failure.retry_after_seconds},
                )
        elif failure.policy == TriggerPolicy.PERMANENT:
            logger.error("provider_configuration_error", extra=log_extra)
            # HF1 1.5 safety net: N consecutive permanent failures with the same category across
            # DIFFERENT contacts mean the provider is the problem even though no single status
            # was clearly a configuration error. The contact that trips it is held, not burned.
            streak = provider_halt.note_permanent_failure(exc.category.value, str(contact.id))
            if prior is not None and streak >= get_settings().provider_halt_systemic_threshold:
                return _hold_for_provider_halt(
                    db, attempt, contact, prior, category=provider_halt.SYSTEMIC_FAILURE,
                    status_code=exc.status_code, log_extra=log_extra,
                    source_category=exc.category.value, count=streak,
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
            resolved, reconcile_note = _reconcile_with_dograh(db, attempt, contact, job)
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
            event_type=_trigger_event_type(failure),
            status_code=exc.status_code,
            category=exc.category.value,
            reconcile=reconcile_note,
            reason_key=failure.reason_key,
            retry_after_seconds=failure.retry_after_seconds,
        )
        return None

    dograh_breaker.record_success()
    provider_halt.note_trigger_accepted()
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
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    reason: NeverConnectedFailureReason,
    *,
    reason_key: str | None = None,
    retry_after_seconds: int | None = None,
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
                attempt,
                contact,
                campaign,
                never_connected=True,
                reason_key=reason_key or reason.value,
                retry_after_seconds=retry_after_seconds,
            )
    except Exception:
        logger.exception("recovery_handling_failed", extra={"attempt_id": str(attempt.id)})
