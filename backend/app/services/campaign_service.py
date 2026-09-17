"""Campaign business logic. Kept intentionally minimal -- no campaign
configuration system is introduced in this checkpoint."""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.campaign import Campaign
from app.repositories.campaign_repository import CampaignRepository
from app.schemas.campaign import CampaignCreate


class CampaignService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.campaigns = CampaignRepository(db)

    def create_campaign(self, data: CampaignCreate) -> Campaign:
        campaign = Campaign(name=data.name)
        return self.campaigns.add(campaign)

    def get_campaign(self, campaign_id: uuid.UUID) -> Campaign:
        campaign = self.campaigns.get_by_id(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return campaign
