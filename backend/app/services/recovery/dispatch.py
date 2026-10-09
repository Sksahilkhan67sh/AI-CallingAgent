"""Recovery dispatch -- Checkpoint 05 §9, §11, §14.

Moves due recovery jobs from the scheduled set onto the *existing*
calls:outbound stream (reusing the CP03 dialer entirely), after
re-checking the state that may have changed since the retry was
decided: contact still active/not suppressed, campaign still active
(not paused, not completed), and the calling window is currently open.
Nothing here ever dials -- it only ever enqueues a DialJob for the
existing dialer worker to process through its own full admission/
eligibility/idempotency pipeline.
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services.audit_service import record_audit_event
from app.services.calling_window import InvalidTimezoneError, NoDialableWindowError
from app.services.queue.enqueue_service import ENQUEUE_GUARD_TTL_SECONDS, enqueue_guard_key
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import DIAL_JOB_KIND, RecoveryScheduler
from app.services.retry_policy_service import (
    get_effective_policy,
    next_open_time,
    window_is_open,
)

logger = logging.getLogger("recovery.dispatch")

_ACTOR = "recovery-dispatcher"
PAUSE_RECHECK_SECONDS = 60  # Step 11 -- how soon to re-check a paused campaign


def dispatch_due_recovery_jobs(
    scheduler: RecoveryScheduler, queue: RedisStreamQueue, *, now: datetime | None = None
) -> int:
    """Called periodically by the worker loop. Returns the number of
    jobs actually enqueued to the dialer this call."""
    now = now or datetime.now(UTC)
    dispatched = 0

    for job_json in scheduler.due_jobs(now):
        if not scheduler.claim(job_json):
            continue  # another worker already claimed this one

        db = SessionLocal()
        try:
            payload = json.loads(job_json)
            if payload.get("kind") == DIAL_JOB_KIND:
                payload.pop("kind")
                dispatched += _process_deferred_dial(
                    db, scheduler, queue, job_json, DialJob(**payload), now
                )
            else:
                dispatched += _process_one(
                    db, scheduler, queue, RecoveryJob.from_json(job_json), now
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("recovery_dispatch_error", extra={"job": job_json})
        finally:
            db.close()

    return dispatched


def _process_one(
    db: Session,
    scheduler: RecoveryScheduler,
    queue: RedisStreamQueue,
    job: RecoveryJob,
    now: datetime,
) -> int:
    contact = db.get(Contact, uuid.UUID(job.contact_id))
    campaign = db.get(Campaign, uuid.UUID(job.campaign_id))
    if contact is None or campaign is None:
        logger.warning("recovery_target_missing", extra={"job_id": job.job_id})
        return 0

    suppressions = SuppressionRepository(db)
    if contact.status == ContactStatus.CLOSED or suppressions.is_suppressed(
        contact.normalized_phone_number
    ):
        _log_skip(db, job, "suppressed")
        return 0

    if campaign.status == CampaignStatus.COMPLETED:
        _log_skip(db, job, "campaign_completed")
        return 0

    if campaign.status == CampaignStatus.PAUSED:
        scheduler.reschedule(job.to_json(), now + timedelta(seconds=PAUSE_RECHECK_SECONDS))
        _log_skip(db, job, "campaign_paused", rescheduled=True)
        return 0

    window_check = _next_window_time(campaign, get_effective_policy(db, campaign), now)
    if window_check is not None:
        scheduler.reschedule(job.to_json(), window_check)
        _log_skip(db, job, "calling_window_closed", rescheduled=True)
        return 0

    # Idempotency backstop: if a CallAttempt for this attempt_number
    # already exists (e.g. a duplicate recovery delivery reconciled
    # elsewhere), don't enqueue a second dial job for it.
    existing = CallAttemptRepository(db).get_by_contact_and_number(
        contact.id, job.attempt_number
    )
    if existing is not None:
        _log_skip(db, job, "attempt_already_exists")
        return 0

    dial_job = DialJob.new(
        campaign_id=campaign.id,
        contact_id=contact.id,
        attempt_number=job.attempt_number,
        recovery_type=job.recovery_type,
        previous_attempt_id=job.attempt_id,
    )
    queue.enqueue(dial_job)

    db.add(
        CallEvent(
            call_attempt_id=uuid.UUID(job.attempt_id),
            event_type="RECONNECT_STARTED",
            payload={"next_attempt_number": job.attempt_number, "trace_id": dial_job.trace_id},
        )
    )
    record_audit_event(
        db,
        actor=_ACTOR,
        action="recovery.dispatched",
        entity_type="contact",
        entity_id=contact.id,
        metadata={"attempt_number": job.attempt_number},
    )
    db.flush()
    return 1


def _log_skip(db: Session, job: RecoveryJob, reason: str, *, rescheduled: bool = False) -> None:
    db.add(
        CallEvent(
            call_attempt_id=uuid.UUID(job.attempt_id),
            event_type="RECOVERY_SKIPPED",
            payload={"reason": reason, "rescheduled": rescheduled},
        )
    )
    db.flush()


def _next_window_time(campaign: Campaign, retry_policy, now: datetime) -> datetime | None:
    """None if dialing is allowed at `now`; otherwise when to look again. Uses the shared
    calling-window functions (campaign timezone, hard-bound clamp). A window that cannot be
    evaluated is never treated as open: the job is re-checked in a minute and the fault is
    logged."""
    try:
        if window_is_open(campaign, retry_policy, now):
            return None
        return next_open_time(campaign, retry_policy, now)
    except (InvalidTimezoneError, NoDialableWindowError):
        logger.error("calling_window_unresolvable", extra={"campaign_id": str(campaign.id)})
        return now + timedelta(seconds=PAUSE_RECHECK_SECONDS)


def _process_deferred_dial(
    db: Session,
    scheduler: RecoveryScheduler,
    queue: RedisStreamQueue,
    job_json: str,
    dial_job: DialJob,
    now: datetime,
) -> int:
    """A first-attempt job that reached the dialer outside its calling window (CP14) comes
    back here when the window opens. PostgreSQL stays the record: only a contact that is
    STILL pending and never attempted is owed this dial, and it is re-checked (suppression,
    campaign state, window) before it goes back on the dialer stream. Nothing dials here."""
    contact = db.get(Contact, uuid.UUID(dial_job.contact_id))
    campaign = db.get(Campaign, uuid.UUID(dial_job.campaign_id))
    if contact is None or campaign is None:
        logger.warning("deferred_dial_target_missing", extra={"job_id": dial_job.job_id})
        return 0
    if contact.status != ContactStatus.PENDING or contact.attempt_count != 0:
        logger.info("deferred_dial_dropped", extra={"job_id": dial_job.job_id, "why": "state"})
        return 0
    if SuppressionRepository(db).is_suppressed(contact.normalized_phone_number):
        logger.info("deferred_dial_dropped", extra={"job_id": dial_job.job_id, "why": "suppressed"})
        return 0
    if campaign.status == CampaignStatus.PAUSED:
        scheduler.reschedule(job_json, now + timedelta(seconds=PAUSE_RECHECK_SECONDS))
        return 0
    if campaign.status != CampaignStatus.ACTIVE:
        logger.info("deferred_dial_dropped", extra={"job_id": dial_job.job_id, "why": "campaign"})
        return 0
    window_check = _next_window_time(campaign, get_effective_policy(db, campaign), now)
    if window_check is not None:
        scheduler.reschedule(job_json, window_check)
        return 0
    # The guard was released when the job was deferred; setting it again means a manual
    # re-enqueue that already queued this contact is not doubled up.
    queued = queue.enqueue_once(
        dial_job, enqueue_guard_key(dial_job.idempotency_key), ENQUEUE_GUARD_TTL_SECONDS
    )
    return 1 if queued else 0
