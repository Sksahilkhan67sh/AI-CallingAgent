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

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CampaignStatus, ContactStatus
from app.models.retry_policy import RetryPolicy
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services.audit_service import record_audit_event
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import RecoveryScheduler

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
            job = RecoveryJob.from_json(job_json)
            dispatched += _process_one(db, scheduler, queue, job, now)
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

    retry_policy = db.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign.id)
    ).scalar_one_or_none()
    if retry_policy is not None:
        window_check = _next_valid_window_time(retry_policy, now)
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


def _next_valid_window_time(retry_policy: RetryPolicy, now: datetime) -> datetime | None:
    """Returns None if `now` is already inside the calling window;
    otherwise the next datetime the window opens."""
    current_time = now.time()
    if retry_policy.window_start <= current_time <= retry_policy.window_end:
        return None

    candidate = now.replace(
        hour=retry_policy.window_start.hour,
        minute=retry_policy.window_start.minute,
        second=retry_policy.window_start.second,
        microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
