"""Contact -- Database Design §2.4.

A contact belongs to exactly one campaign (CAMPAIGN ||--o{ CONTACT in the
canonical ER diagram) -- there is no separate campaign-membership join
table in the reconciled spec, so none is introduced here. See
docs/CHECKPOINT-01-NOTES.md for the reasoning.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
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
        # P1-2: the rebuild scans only contacts that carry a durable retry schedule.
        Index(
            "ix_contact_next_retry_at",
            "next_retry_at",
            postgresql_where=text("next_retry_at IS NOT NULL"),
        ),
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
    # P1-2: PostgreSQL is the source of truth for a scheduled retry; the Redis ZSET is only a
    # cache rebuilt from these three columns. Set together by RecoveryManager when a retry is
    # scheduled; cleared when the dialer claims the retry (contact -> DIALING).
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_attempt_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    retry_attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CP15: legal hold blocks every retention/erasure action on this contact's data.
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # CP15: set when the phone fields were scrubbed (retention or erasure).
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
