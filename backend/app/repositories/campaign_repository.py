"""Campaign persistence."""

import uuid

from sqlalchemy.orm import Session

from app.models.campaign import Campaign


class CampaignRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, campaign_id: uuid.UUID) -> Campaign | None:
        return self.db.get(Campaign, campaign_id)

    def add(self, campaign: Campaign) -> Campaign:
        self.db.add(campaign)
        self.db.flush()
        return campaign
