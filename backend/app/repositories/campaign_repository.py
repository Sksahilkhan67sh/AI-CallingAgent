"""Campaign persistence."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import CampaignStatus


class CampaignRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, campaign_id: uuid.UUID) -> Campaign | None:
        return self.db.get(Campaign, campaign_id)

    def add(self, campaign: Campaign) -> Campaign:
        self.db.add(campaign)
        self.db.flush()
        return campaign

    def list(
        self, *, status: CampaignStatus | None = None, limit: int, offset: int
    ) -> tuple[list[Campaign], int]:
        filters = []
        if status is not None:
            filters.append(Campaign.status == status)

        total = self.db.execute(
            select(func.count()).select_from(Campaign).where(*filters)
        ).scalar_one()

        stmt = (
            select(Campaign)
            .where(*filters)
            .order_by(Campaign.created_at.desc(), Campaign.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self.db.execute(stmt).scalars())
        return items, total
