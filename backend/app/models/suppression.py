"""Suppression -- Database Design §2.12.

The single canonical DNC/opt-out table. Checked by phone number as well
as contact_id, so a contact re-imported under a new contact_id in a
future campaign is still caught (spec note). Every write to this table
should be audit-logged (Security Architecture §9); enforced at the
service layer, not here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import SuppressionSource


class Suppression(Base):
    __tablename__ = "suppression"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contact.id"), primary_key=True
    )
    phone_number: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[SuppressionSource] = mapped_column(
        pg_enum(SuppressionSource, "suppression_source"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
