"""Campaign business logic. Kept intentionally minimal -- no campaign
configuration system is introduced in this checkpoint."""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.repositories.campaign_repository import CampaignRepository
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.services.audit_service import record_audit_event

_ACTOR = "api-client"

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
    def __init__(self, db: Session) -> None:
        self.db = db
        self.campaigns = CampaignRepository(db)

    def create_campaign(self, data: CampaignCreate) -> Campaign:
        campaign = Campaign(name=data.name)
        campaign = self.campaigns.add(campaign)
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="campaign.created",
            entity_type="campaign",
            entity_id=campaign.id,
        )
        return campaign

    def get_campaign(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = self.campaigns.get_by_id(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return campaign

    def list_campaigns(
        self, *, status: CampaignStatus | None, limit: int, offset: int
    ) -> tuple[list[Campaign], int]:
        return self.campaigns.list(status=status, limit=limit, offset=offset)

    def update_campaign(self, campaign_id: uuid.UUID, data: CampaignUpdate) -> Campaign:
        campaign = self.get_campaign(campaign_id)

        if data.name is not None and data.name != campaign.name:
            campaign.name = data.name
            record_audit_event(
                self.db,
                actor=_ACTOR,
                action="campaign.updated",
                entity_type="campaign",
                entity_id=campaign.id,
                metadata={"field": "name"},
            )

        if data.status is not None and data.status != campaign.status:
            self._transition_status(campaign, data.status)

        self.db.flush()
        return campaign

    def _transition_status(self, campaign: Campaign, new_status: CampaignStatus) -> None:
        allowed = _ALLOWED_TRANSITIONS[campaign.status]
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition campaign from {campaign.status.value} to "
                f"{new_status.value}"
            )
        old_status = campaign.status
        campaign.status = new_status
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="campaign.status_changed",
            entity_type="campaign",
            entity_id=campaign.id,
            metadata={"from": old_status.value, "to": new_status.value},
        )
