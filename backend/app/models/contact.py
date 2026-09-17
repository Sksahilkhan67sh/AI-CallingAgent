"""Contact -- Database Design §2.4.

A contact belongs to exactly one campaign (CAMPAIGN ||--o{ CONTACT in the
canonical ER diagram) -- there is no separate campaign-membership join
table in the reconciled spec, so none is introduced here. See
docs/CHECKPOINT-01-NOTES.md for the reasoning.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import ContactStatus


class Contact(Base):
    __tablename__ = "contact"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "normalized_phone_number", name="uq_contact_campaign_phone"
        ),
        Index("ix_contact_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaign.id"), nullable=False
    )
    phone_number: Mapped[str] = mapped_column(String, nullable=False)
    normalized_phone_number: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[ContactStatus] = mapped_column(
        pg_enum(ContactStatus, "contact_status"),
        nullable=False,
        default=ContactStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
