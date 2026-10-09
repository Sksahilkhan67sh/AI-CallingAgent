"""Campaign -- Database Design §2.1."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
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
    # CP14: the IANA zone the campaign's calling window is read in, and the region used to
    # parse numbers written without a country code. Single-tenant defaults.
    timezone: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'Asia/Kolkata'")
    )
    default_region: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'IN'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
