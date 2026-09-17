"""Conversation persistence foundation.

`ConversationSession` and `ConversationMessage` give later checkpoints
somewhere to write turn-by-turn conversation state *while a call is in
progress* -- distinct from `call_attempt` (the attempt record) and from
the eventual compiled `transcript` (Database Design §2.8, a later
checkpoint). `CallEvent` is a generic per-attempt event log, distinct
from the disconnect-specific `disconnect_event` table (also a later
checkpoint). No real-time voice loop, STT/LLM/TTS integration, or event
producers are implemented here -- persistence only.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import ConversationRole, ConversationSessionStatus


class ConversationSession(Base):
    __tablename__ = "conversation_session"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    call_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("call_attempt.id"), nullable=False, unique=True
    )
    status: Mapped[ConversationSessionStatus] = mapped_column(
        pg_enum(ConversationSessionStatus, "conversation_session_status"),
        nullable=False,
        default=ConversationSessionStatus.ACTIVE,
    )
    started_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ConversationMessage(Base):
    __tablename__ = "conversation_message"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_conversation_message_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_session.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[ConversationRole] = mapped_column(
        pg_enum(ConversationRole, "conversation_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )


class CallEvent(Base):
    __tablename__ = "call_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    call_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("call_attempt.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
