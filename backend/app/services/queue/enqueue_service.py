"""Enqueue eligible campaign contacts -- Checkpoint 03 Steps 24-27.

Synchronous, bounded, and paged (Step 25) -- no background job system
exists yet, so this must stay safe to run inline within one HTTP request.

CP12-C (H2): the scan is KEYSET-paginated on the contact primary key. It used to be
LIMIT/OFFSET over "status = PENDING" ordered by created_at: workers flip contacts out of
PENDING while the scan runs, so the filtered set shrank under the offset (rows were jumped
over) and the per-page total shrank under the stop test -- ~700 of ~1200 contacts got queued.

Scope: only ever enqueues attempt_number=1, for Pending contacts that
have never been attempted (attempt_count == 0). Retrying a contact that
already has an attempt is explicitly a later checkpoint's job (Step 18).
"""

import logging
import time
import uuid

import redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError
from app.core.request_context import current_request_id
from app.models.enums import CampaignStatus
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.queue import EnqueueResult
from app.services import kill_switch
from app.services.audit_service import record_audit_event
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue

# Newly queued jobs per request. Reaching it is reported (complete=False), never silent.
MAX_ENQUEUE_PER_REQUEST = 100_000
# Soft, best-effort duplicate-enqueue guard (Step 26). The database's
# unique (contact_id, attempt_number) constraint, enforced when the
# worker actually claims/creates the CallAttempt, remains the final
# protection -- this just avoids obviously re-enqueueing the same
# contact within a short window.
ENQUEUE_GUARD_TTL_SECONDS = 3600

_ACTOR = "api-client"

logger = logging.getLogger("enqueue_service")


def enqueue_guard_key(idempotency_key: str) -> str:
    return f"enqueued:{idempotency_key}"


class QueueEnqueueService:
    def __init__(self, db: Session, queue: RedisStreamQueue, actor: str = _ACTOR) -> None:
        self.db = db
        self.actor = actor
        self.queue = queue
        self.campaigns = CampaignRepository(db)
        self.contacts = ContactRepository(db)
        self.suppressions = SuppressionRepository(db)

    def enqueue_campaign(self, campaign_id: uuid.UUID) -> EnqueueResult:
        campaign = self.campaigns.get_by_id(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        if campaign.status != CampaignStatus.ACTIVE:
            raise ValidationError(
                f"Campaign is {campaign.status.value}, not active -- cannot enqueue"
            )
        # CP11: no new outbound work while the global kill switch is on, and an
        # unreadable switch is treated as on (fail closed -> 503, not a silent 500).
        # Jobs already queued are untouched; the dial worker gates them again.
        blocked = kill_switch.block_reason()
        if blocked == kill_switch.UNAVAILABLE:
            raise ServiceUnavailableError("Outbound control state is unavailable")
        if blocked is not None:
            # Committed before raising: get_db rolls back on an exception, which would
            # otherwise discard this row.
            record_audit_event(
                self.db,
                actor=self.actor,
                action="campaign.enqueue_blocked",
                entity_type="campaign",
                entity_id=campaign_id,
                metadata={"reason": blocked, "request_id": current_request_id()},
            )
            self.db.commit()
            raise ConflictError("Outbound calling is disabled by the global kill switch")

        page_size = get_settings().enqueue_page_size
        discovered = enqueued = skipped_suppressed = skipped_duplicate = pages = 0
        complete = True
        cursor: uuid.UUID | None = None
        started = time.monotonic()

        try:
            while complete:
                page = self.contacts.eligible_enqueue_page(
                    campaign_id, after_id=cursor, limit=page_size
                )
                if not page:
                    break  # the ONLY way a scan ends: an empty page, never a short one
                suppressed = self.suppressions.suppressed_among({phone for _, phone in page})
                # End the read transaction: nothing below may run inside one (Redis I/O).
                self.db.commit()

                for contact_id, phone in page:
                    if enqueued >= MAX_ENQUEUE_PER_REQUEST:
                        complete = False
                        break
                    if phone in suppressed:
                        skipped_suppressed += 1
                    else:
                        job = DialJob.new(
                            campaign_id=campaign_id, contact_id=contact_id, attempt_number=1
                        )
                        if self.queue.enqueue_once(
                            job, enqueue_guard_key(job.idempotency_key), ENQUEUE_GUARD_TTL_SECONDS
                        ):
                            enqueued += 1
                        else:
                            skipped_duplicate += 1
                    discovered += 1

                # Advance only after the whole page was handled; an exception above leaves
                # the cursor (and the counts) exactly at what was truly processed.
                if complete:
                    cursor = page[-1][0]
                pages += 1
                logger.info(
                    "campaign_enqueue_page",
                    extra={
                        "campaign_id": str(campaign_id),
                        "page": pages,
                        "page_size": page_size,
                        "rows": len(page),
                        "discovered": discovered,
                        "enqueued": enqueued,
                        "skipped_suppressed": skipped_suppressed,
                        "skipped_duplicate": skipped_duplicate,
                        "cursor": str(cursor) if cursor else None,
                    },
                )
        except redis.RedisError as exc:
            # Truthful partial result: what was queued stays queued (and guarded), the rest is
            # untouched, and a re-run is idempotent. Never report a success.
            self._record_failure(campaign_id, pages, discovered, enqueued, exc)
            raise ServiceUnavailableError(
                f"Enqueue interrupted by a queue error after queueing {enqueued} job(s) "
                f"({discovered} contact(s) handled); re-running it is safe"
            ) from exc
        except SQLAlchemyError as exc:
            logger.error(
                "campaign_enqueue_failed",
                extra={
                    "campaign_id": str(campaign_id),
                    "pages": pages,
                    "discovered": discovered,
                    "enqueued": enqueued,
                    "error_type": type(exc).__name__,
                },
            )
            raise

        record_audit_event(
            self.db,
            actor=self.actor,
            action="campaign.enqueued",
            entity_type="campaign",
            entity_id=campaign_id,
            metadata={
                "enqueued": enqueued,
                "skipped_suppressed": skipped_suppressed,
                "skipped_duplicate": skipped_duplicate,
                "discovered": discovered,
                "pages_processed": pages,
                "complete": complete,
            },
        )
        logger.info(
            "campaign_enqueue_completed",
            extra={
                "campaign_id": str(campaign_id),
                "pages": pages,
                "discovered": discovered,
                "enqueued": enqueued,
                "complete": complete,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return EnqueueResult(
            campaign_id=campaign_id,
            enqueued=enqueued,
            skipped_suppressed=skipped_suppressed,
            skipped_duplicate=skipped_duplicate,
            discovered=discovered,
            pages_processed=pages,
            complete=complete,
        )

    def _record_failure(
        self, campaign_id: uuid.UUID, pages: int, discovered: int, enqueued: int, exc: Exception
    ) -> None:
        logger.error(
            "campaign_enqueue_failed",
            extra={
                "campaign_id": str(campaign_id),
                "pages": pages,
                "discovered": discovered,
                "enqueued": enqueued,
                "error_type": type(exc).__name__,
            },
        )
        # Committed before raising: get_db rolls back on an exception.
        record_audit_event(
            self.db,
            actor=self.actor,
            action="campaign.enqueue_failed",
            entity_type="campaign",
            entity_id=campaign_id,
            metadata={
                "enqueued": enqueued,
                "discovered": discovered,
                "pages_processed": pages,
                "request_id": current_request_id(),
            },
        )
        self.db.commit()


def requeue_after_resume(
    db: Session, queue: RedisStreamQueue, campaign_id: uuid.UUID, *, actor: str
) -> EnqueueResult | None:
    """CP12-B: called AFTER a PAUSED -> ACTIVE transition has been committed (never inside
    its transaction: no DB lock is held across Redis calls).

    While a campaign was paused, workers dropped the Redis reference of every first-attempt
    job they consumed -- and its enqueue guard -- because Postgres (contact PENDING, no
    attempt) is the durable record. This re-runs the ordinary idempotent enqueue: contacts
    that still have a live reference keep their guard and are skipped, so only the dropped
    ones are queued again, exactly once.

    The resume itself has already succeeded, so a refusal here is not an error of the resume:
    the global kill switch, a campaign paused again in the meantime, or Redis being down all
    leave the contacts PENDING and unguarded for the next `POST .../enqueue`."""
    try:
        return QueueEnqueueService(db, queue, actor=actor).enqueue_campaign(campaign_id)
    except (ConflictError, ServiceUnavailableError, ValidationError) as exc:
        logger.warning(
            "campaign_resume_requeue_deferred",
            extra={"campaign_id": str(campaign_id), "reason": type(exc).__name__},
        )
    except redis.RedisError as exc:
        logger.error(
            "campaign_resume_requeue_failed",
            extra={"campaign_id": str(campaign_id), "error_type": type(exc).__name__},
        )
    return None
