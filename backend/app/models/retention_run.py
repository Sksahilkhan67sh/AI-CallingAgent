"""RetentionRun -- CP15A. One row per `run_retention` invocation (dry-run or apply)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class RetentionRun(Base):
    __tablename__ = "retention_run"
    __table_args__ = (
        Index("ix_retention_run_started_at", "started_at"),
        CheckConstraint("mode IN ('dry_run', 'apply')", name="ck_retention_run_mode"),
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_retention_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    # Per-class counts only -- never identifiers or PII.
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Sanitized category (e.g. "lock_held", "db_error"); never raw exception text.
    error_category: Mapped[str | None] = mapped_column(String, nullable=True)
