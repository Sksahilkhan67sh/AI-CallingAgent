"""Persist a transcript fetch result on its conversation session (P1-8).

Shared by the Dograh webhook (first fetch) and the analysis worker (re-fetch of an UNAVAILABLE
transcript), so both record the outcome the same way:

  OK           -> the lines become ConversationMessage rows; status 'ok'
  EMPTY        -> no rows; status 'empty'   (a real fact: the call had no transcript content)
  UNAVAILABLE  -> no rows; status 'unavailable' + a sanitized reason. NOT an empty transcript.
                  The signed link is kept only if the reason is retryable, so it can be re-fetched.
"""

from sqlalchemy.orm import Session

from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import ConversationRole
from app.services.telephony.transcript_fetch import (
    TranscriptKind,
    TranscriptResult,
    fetch_transcript,
    is_retryable,
)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"


def role_for(raw_role: str) -> ConversationRole:
    text = raw_role.lower()
    if any(k in text for k in ("user", "customer", "caller", "human")):
        return ConversationRole.CONTACT
    if any(k in text for k in ("assistant", "agent", "bot", "ai")):
        return ConversationRole.AGENT
    return ConversationRole.SYSTEM


def apply_transcript_result(
    db: Session, session: ConversationSession, result: TranscriptResult, url: str
) -> None:
    if result.kind == TranscriptKind.UNAVAILABLE:
        session.transcript_status = STATUS_UNAVAILABLE
        session.transcript_unavailable_reason = result.reason
        session.transcript_url = url if is_retryable(result.reason) else None
        return

    for i, line in enumerate(result.lines, start=1):
        raw_role = str(line.get("role") or line.get("speaker") or "")
        content = str(line.get("content") or line.get("text") or line.get("message") or "").strip()
        if not content:
            continue
        db.add(
            ConversationMessage(
                session_id=session.id, sequence=i, role=role_for(raw_role), content=content
            )
        )
    session.transcript_status = (
        STATUS_OK if result.kind == TranscriptKind.OK else STATUS_EMPTY
    )
    session.transcript_unavailable_reason = None
    session.transcript_url = None
    db.flush()


def refetch_unavailable(db: Session, session: ConversationSession) -> TranscriptResult | None:
    """For an UNAVAILABLE session with a retained link: fetch again and record the new outcome.
    Returns the new result, or None when there is nothing to retry (no link kept)."""
    if session.transcript_status != STATUS_UNAVAILABLE or not session.transcript_url:
        return None
    url = session.transcript_url
    result = fetch_transcript(url)
    apply_transcript_result(db, session, result, url)
    return result
