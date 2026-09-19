"""Enqueue eligible campaign contacts -- Checkpoint 03 Steps 24-27.

Synchronous, bounded, and chunked (Step 25) -- the same pattern
Checkpoint 02's bulk import used for the same reason: no background job
system exists yet, so this must stay safe to run inline within one
HTTP request rather than needing one.

Scope: only ever enqueues attempt_number=1, for Pending contacts that
have never been attempted (attempt_count == 0). Retrying a contact that
already has an attempt is explicitly a later checkpoint's job (Step 18).
"""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.queue import EnqueueResult
from app.services.audit_service import record_audit_event
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue

CHUNK_SIZE = 500
MAX_ENQUEUE_PER_REQUEST = 100_000
# Soft, best-effort duplicate-enqueue guard (Step 26). The database's
# unique (contact_id, attempt_number) constraint, enforced when the
# worker actually claims/creates the CallAttempt, remains the final
# protection -- this just avoids obviously re-enqueueing the same
# contact within a short window.
_ENQUEUE_GUARD_TTL_SECONDS = 3600

_ACTOR = "api-client"


class QueueEnqueueService:
    def __init__(self, db: Session, queue: RedisStreamQueue) -> None:
        self.db = db
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

        enqueued = 0
        skipped_suppressed = 0
        skipped_duplicate = 0
        offset = 0

        while True:
            items, total = self.contacts.list(
                campaign_id=campaign_id,
                status=ContactStatus.PENDING,
                limit=CHUNK_SIZE,
                offset=offset,
            )
            if not items:
                break

            for contact in items:
                if enqueued + skipped_suppressed + skipped_duplicate >= MAX_ENQUEUE_PER_REQUEST:
                    break

                if contact.attempt_count > 0:
                    continue  # retries are out of scope this checkpoint

                if self.suppressions.is_suppressed(contact.normalized_phone_number):
                    skipped_suppressed += 1
                    continue

                job = DialJob.new(
                    campaign_id=campaign_id, contact_id=contact.id, attempt_number=1
                )

                guard_key = f"enqueued:{job.idempotency_key}"
                if not self.queue.redis.set(
                    guard_key, "1", nx=True, ex=_ENQUEUE_GUARD_TTL_SECONDS
                ):
                    skipped_duplicate += 1
                    continue

                self.queue.enqueue(job)
                enqueued += 1

            offset += CHUNK_SIZE
            if offset >= total:
                break

        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="campaign.enqueued",
            entity_type="campaign",
            entity_id=campaign.id,
            metadata={
                "enqueued": enqueued,
                "skipped_suppressed": skipped_suppressed,
                "skipped_duplicate": skipped_duplicate,
            },
        )

        return EnqueueResult(
            campaign_id=campaign.id,
            enqueued=enqueued,
            skipped_suppressed=skipped_suppressed,
            skipped_duplicate=skipped_duplicate,
        )
