"""Campaign business logic. Kept intentionally minimal -- no campaign
configuration system is introduced in this checkpoint."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.request_context import current_request_id
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.repositories.campaign_repository import CampaignRepository
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.services.audit_service import record_audit_event

_ACTOR = "api-client"

logger = logging.getLogger("campaign_service")

# No explicit campaign-lifecycle transition table exists in
# docs/specs/Backend/Database-Design.md beyond naming the four states,
# so this is the checkpoint's own, deliberately conservative reading:
# forward progress, pause/resume, and a one-way move to Completed.
# Completed is terminal. Draft cannot jump straight to Completed --
# nothing has run yet, so there's nothing to complete.
_ALLOWED_TRANSITIONS: dict[CampaignStatus, set[CampaignStatus]] = {
    CampaignStatus.DRAFT: {CampaignStatus.ACTIVE},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.COMPLETED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.COMPLETED},
    CampaignStatus.COMPLETED: set(),
}


class CampaignService:
    def __init__(self, db: Session, actor: str = _ACTOR) -> None:
        self.db = db
        self.actor = actor
        self.campaigns = CampaignRepository(db)
        # CP12-B: set when update_campaign moved PAUSED -> ACTIVE, so the caller can re-queue
        # AFTER committing (the lock and the transaction must not span Redis calls).
        self.resumed = False

    def create_campaign(self, data: CampaignCreate) -> Campaign:
        campaign = Campaign(name=data.name)
        campaign = self.campaigns.add(campaign)
        record_audit_event(
            self.db,
            actor=self.actor,
            action="campaign.created",
            entity_type="campaign",
            entity_id=campaign.id,
        )
        return campaign

    def get_campaign(self, campaign_id: uuid.UUID, *, for_update: bool = False) -> Campaign:
        campaign = self.campaigns.get_by_id(campaign_id, for_update=for_update)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return campaign

    def list_campaigns(
        self, *, status: CampaignStatus | None, limit: int, offset: int
    ) -> tuple[list[Campaign], int]:
        return self.campaigns.list(status=status, limit=limit, offset=offset)

    def update_campaign(self, campaign_id: uuid.UUID, data: CampaignUpdate) -> Campaign:
        # CP12-B: row lock, so concurrent pause/resume requests serialize in the database and
        # each one validates against the committed status (no lost update, no impossible
        # audit trail). Held only for this short transaction -- no network call happens here.
        campaign = self.get_campaign(campaign_id, for_update=True)

        if data.name is not None and data.name != campaign.name:
            campaign.name = data.name
            record_audit_event(
                self.db,
                actor=self.actor,
                action="campaign.updated",
                entity_type="campaign",
                entity_id=campaign.id,
                metadata={"field": "name"},
            )

        if data.status is not None and data.status != campaign.status:
            self._transition_status(campaign, data.status)
        elif data.status is not None:
            logger.info(
                "campaign_transition_noop",
                extra={
                    "campaign_id": str(campaign.id),
                    "status": campaign.status.value,
                },
            )

        self.db.flush()
        return campaign

    def _transition_status(self, campaign: Campaign, new_status: CampaignStatus) -> None:
        old_status = campaign.status
        request_id = current_request_id()
        event_fields = {
            "campaign_id": str(campaign.id),
            "from": old_status.value,
            "to": new_status.value,
            "actor": self.actor,
        }  # request_id is added to every log record by the request-context log filter
        if new_status == CampaignStatus.ACTIVE and old_status == CampaignStatus.PAUSED:
            logger.info("campaign_resume_requested", extra=event_fields)
        if new_status not in _ALLOWED_TRANSITIONS[old_status]:
            if new_status == CampaignStatus.PAUSED:
                logger.warning("campaign_pause_transition_rejected", extra=event_fields)
            elif new_status == CampaignStatus.ACTIVE:
                logger.warning("campaign_resume_transition_rejected", extra=event_fields)
            raise ValidationError(
                f"Cannot transition campaign from {old_status.value} to {new_status.value}"
            )
        campaign.status = new_status
        record_audit_event(
            self.db,
            actor=self.actor,
            action="campaign.status_changed",
            entity_type="campaign",
            entity_id=campaign.id,
            metadata={
                "from": old_status.value,
                "to": new_status.value,
                "request_id": request_id,
                "result": "applied",
            },
        )
        if new_status == CampaignStatus.PAUSED:
            logger.info("campaign_paused", extra=event_fields)
        elif old_status == CampaignStatus.PAUSED and new_status == CampaignStatus.ACTIVE:
            self.resumed = True
            logger.info("campaign_resumed", extra=event_fields)
