"""Transcript preparation -- Checkpoint 06 §10-11, §30.

Loads the immutable ConversationMessage rows for a session (chronological
by `sequence`, the existing ordering column -- Checkpoint 04), and
normalizes them into the text the LLM sees. This never mutates
ConversationMessage rows and never fabricates dialogue; it only cleans
whitespace/empty artifacts and bounds size for cost control (§30).

Strict separation from analysis (§11): this module produces WHAT WAS
SAID, prepared for reading -- it has no opinion on what any of it means.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.conversation import ConversationMessage, ConversationSession


@dataclass
class PreparedTranscript:
    lines: list[str]  # "role: content", chronological
    message_count: int
    duration_seconds: float | None
    truncated: bool


def load_conversation_session(
    db: Session, call_attempt_id
) -> ConversationSession | None:
    return (
        db.execute(
            select(ConversationSession).where(
                ConversationSession.call_attempt_id == call_attempt_id
            )
        )
        .scalars()
        .one_or_none()
    )


def prepare_transcript(db: Session, session: ConversationSession) -> PreparedTranscript:
    rows = (
        db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session.id)
            .order_by(ConversationMessage.sequence)
        )
        .scalars()
        .all()
    )

    lines: list[str] = []
    for row in rows:
        content = " ".join(row.content.split())  # collapse duplicate whitespace
        if not content:
            continue  # malformed/empty message artifact -- dropped, not fabricated
        lines.append(f"{row.role.value}: {content}")

    max_messages = get_settings().analysis_max_transcript_messages
    truncated = False
    if len(lines) > max_messages:
        # §30: preserve beginning, ending, and outcome over the noisy
        # middle when truncation is unavoidable -- keep the first third
        # and the last two-thirds of the budget from the end, which
        # skews toward the ending/outcome where the disposition usually
        # becomes clear, while still keeping the opening for context.
        head = max_messages // 3
        tail = max_messages - head
        lines = lines[:head] + lines[-tail:]
        truncated = True

    duration_seconds = None
    if session.ended_at is not None:
        duration_seconds = (session.ended_at - session.started_at).total_seconds()

    return PreparedTranscript(
        lines=lines,
        message_count=len(rows),
        duration_seconds=duration_seconds,
        truncated=truncated,
    )
