"""OutboundControl -- HF1 1.3.

Durable global "stop dialing" flags. PostgreSQL is the source of truth (Redis is a cache
that can be flushed or restarted without persistence; a stop that lives only there silently
turns OFF). One row per control, created lazily on first engage:

* `kill_switch`   -- the operator's global kill switch.
* `provider_halt` -- HF1 1.5: engaged automatically when the provider is misconfigured or
                    failing systemically; released only by an admin.

A missing row means "not engaged". Reasons are sanitized, short, free of credentials.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboundControl(Base):
    __tablename__ = "outbound_control"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    engaged_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
