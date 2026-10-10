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

    settings = get_settings()
    max_message_chars = settings.analysis_max_message_chars
    truncated = False

    lines: list[str] = []
    for row in rows:
        content = " ".join(row.content.split())  # collapse duplicate whitespace
        if not content:
            continue  # malformed/empty message artifact -- dropped, not fabricated
        if len(content) > max_message_chars:
            content = content[:max_message_chars]  # bound each message; flagged below
            truncated = True
        lines.append(f"{row.role.value}: {content}")

    max_messages = settings.analysis_max_transcript_messages
    if len(lines) > max_messages:
        # §30: preserve beginning, ending, and outcome over the noisy middle when truncation
        # is unavoidable -- keep the first third and the remaining budget from the end,
        # which skews toward the ending/outcome where the disposition usually becomes clear.
        head = max_messages // 3
        tail = max_messages - head
        lines = lines[:head] + lines[-tail:]
        truncated = True

    # CP14B: a total-character bound too (message counts alone do not bound size). Keep the
    # opening (1/3 of the budget) and the ending (2/3), drop the middle. Characters are NOT an
    # exact token guarantee.
    max_chars = settings.analysis_max_transcript_chars
    if sum(len(line) for line in lines) > max_chars:
        lines = _fit_chars(lines, max_chars)
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


def _fit_chars(lines: list[str], budget: int) -> list[str]:
    head_budget = budget // 3
    tail_budget = budget - head_budget
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > head_budget:
            break
        head.append(line)
        used += len(line)
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) > tail_budget:
            break
        tail.append(line)
        used += len(line)
    tail.reverse()
    return head + tail
