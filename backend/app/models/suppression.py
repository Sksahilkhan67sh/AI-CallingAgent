"""Suppression -- Database Design §2.12, restructured in CP14.

The single canonical, GLOBAL do-not-call table: one row per normalized number, checked by
number on every dial path, so a number is blocked in every campaign. Before CP14 the
primary key was `contact_id`, which made it impossible to store a do-not-call number that
has no contact (a registry-scrubbed list, an operator-added number). Now:

* `id` is the primary key;
* `phone_number` (normalized E.164) is UNIQUE -- the real identity of a row;
* `contact_id` is optional (still a FK) and unique when present;
* `created_by` records who added it (NULL for provider/AI-driven opt-outs).

Every write is audit-logged at the service layer, never with the full number.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import SuppressionSource


class Suppression(Base):
    __tablename__ = "suppression"
    __table_args__ = (
        UniqueConstraint("phone_number", name="uq_suppression_phone_number"),
        UniqueConstraint("contact_id", name="uq_suppression_contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contact.id"), nullable=True
    )
    phone_number: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[SuppressionSource] = mapped_column(
        pg_enum(SuppressionSource, "suppression_source"), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
