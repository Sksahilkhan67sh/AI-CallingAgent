"""AuditLog -- production traceability foundation (Security Architecture §9).

Never store secrets, tokens, or full request bodies here -- `event_metadata`
is for small, purpose-built context (e.g. before/after values for a
config change), not a dumping ground.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        # "audit logs by entity" -- e.g. the full history for one contact
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        # "audit logs by ... time" -- recent-activity queries
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
