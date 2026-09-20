"""WorkingMemorySnapshot -- Database Design §2.6.

Durable checkpoint of the *compact structured state* needed to resume
or analyze a conversation -- explicitly not the transcript (that's
ConversationMessage) and not the full turn log (Memory-Specification.md
§2's `turns` field is served by reading ConversationMessage rows, not
duplicated here). See docs/CHECKPOINT-04-NOTES.md.

One row per snapshot write; Database-Design.md §2.5's invariant note
("one active working-memory snapshot per attempt... latest snapshot per
attempt_id") is a read-time concern for whoever loads the latest
snapshot, not a uniqueness constraint here -- keeping every snapshot
gives Checkpoint 04's memory-checkpoint-after-every-turn requirement
(Step 13) a full history for auditability, which a single-overwritten
row would lose.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import RecordingConsent


class WorkingMemorySnapshot(Base):
    __tablename__ = "working_memory_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("call_attempt.id"), nullable=False, index=True
    )
    captured_entities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    script_progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    objections_raised: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_agent_utterance: Mapped[str | None] = mapped_column(Text, nullable=True)
    disconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_suppression: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recording_consent: Mapped[RecordingConsent] = mapped_column(
        pg_enum(RecordingConsent, "recording_consent"),
        nullable=False,
        default=RecordingConsent.NOT_APPLICABLE,
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    # clock_timestamp(), not now(): now()/CURRENT_TIMESTAMP is frozen at
    # transaction start, so multiple snapshots checkpointed within one
    # transaction (the common case -- one worker, one DB session driving
    # a whole conversation) would all get the identical value, making
    # "the latest snapshot" ambiguous. clock_timestamp() advances on
    # every call. (Caught by test_load_latest_returns_the_most_recent_snapshot.)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
