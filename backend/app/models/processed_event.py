"""ProcessedEvent -- webhook idempotency foundation (Webhook-Specification.md §5).

Idempotency is enforced by recording processed `event_id` values and
short-circuiting duplicates before any state-changing action. This
checkpoint establishes the persistence/uniqueness constraint only -- the
webhook handler itself is a later checkpoint.
"""

import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
