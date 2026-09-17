"""Campaign -- Database Design §2.1."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import CampaignStatus


class Campaign(Base):
    __tablename__ = "campaign"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        pg_enum(CampaignStatus, "campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
        index=True,  # "campaigns by ... status"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
