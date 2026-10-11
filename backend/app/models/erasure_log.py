"""ErasureLog -- CP15A. Append-only record that an erasure request was executed.

Holds a keyed fingerprint (HMAC-SHA256 of the normalized number with a server-side pepper),
NEVER the number. `reapply_erasures` uses it after a database restore so erased people do not
come back from a backup. UPDATE and DELETE are rejected by a database trigger.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ErasureLog(Base):
    __tablename__ = "erasure_log"
    __table_args__ = (Index("ix_erasure_log_fingerprint", "fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
