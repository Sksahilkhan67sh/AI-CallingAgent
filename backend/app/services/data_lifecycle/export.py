"""Single-contact data export -- CP15A (data-subject access). Size-bounded and audited.
The export deliberately contains the person's own data (number, turns, analysis text); the
audit row records only counts and ids."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationSession
from app.services.audit_service import record_audit_event

MAX_EXPORT_ATTEMPTS = 200
MAX_EXPORT_MESSAGES = 5000


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def export_contact(db: Session, *, contact_id: uuid.UUID, actor: str) -> dict[str, Any]:
    contact = db.get(Contact, contact_id, populate_existing=True)  # bulk scrubs bypass the ORM
    if contact is None:
        raise NotFoundError(f"Contact {contact_id} not found")

    attempts = list(
        db.execute(
            select(CallAttempt)
            .where(CallAttempt.contact_id == contact_id)
            .order_by(CallAttempt.attempt_number)
            .limit(MAX_EXPORT_ATTEMPTS + 1)
        ).scalars()
    )
    truncated = len(attempts) > MAX_EXPORT_ATTEMPTS
    attempts = attempts[:MAX_EXPORT_ATTEMPTS]
    attempt_ids = [a.id for a in attempts]

    budget = MAX_EXPORT_MESSAGES
    messages_by_attempt: dict[uuid.UUID, list[dict[str, Any]]] = {a: [] for a in attempt_ids}
    if attempt_ids:
        rows = db.execute(
            select(ConversationSession.call_attempt_id, ConversationMessage)
            .join(ConversationMessage, ConversationMessage.session_id == ConversationSession.id)
            .where(ConversationSession.call_attempt_id.in_(attempt_ids))
            .order_by(ConversationSession.call_attempt_id, ConversationMessage.sequence)
            .limit(budget + 1)
        ).all()
        if len(rows) > budget:
            truncated = True
            rows = rows[:budget]
        for attempt_id, message in rows:
            messages_by_attempt[attempt_id].append(
                {
                    "sequence": message.sequence,
                    "role": message.role.value,
                    "content": message.content,
                    "created_at": _iso(message.created_at),
                }
            )

    analyses = {
        a.call_attempt_id: a
        for a in db.execute(
            select(CallAnalysis).where(CallAnalysis.call_attempt_id.in_(attempt_ids))
        ).scalars()
    }

    out_attempts = []
    message_total = 0
    for attempt in attempts:
        analysis = analyses.get(attempt.id)
        turns = messages_by_attempt[attempt.id]
        message_total += len(turns)
        out_attempts.append(
            {
                "id": str(attempt.id),
                "attempt_number": attempt.attempt_number,
                "state": attempt.state.value,
                "started_at": _iso(attempt.started_at),
                "ended_at": _iso(attempt.ended_at),
                "duration_seconds": attempt.duration_seconds,
                "recording_consent": (
                    attempt.recording_consent.value if attempt.recording_consent else None
                ),
                "transcript": turns,
                "analysis": None
                if analysis is None
                else {
                    "status": analysis.status.value,
                    "summary": analysis.summary,
                    "feedback": analysis.feedback,
                    "key_facts": analysis.key_facts,
                    "objections": analysis.objections,
                    "customer_needs": analysis.customer_needs,
                    "intent": analysis.intent.value if analysis.intent else None,
                    "sentiment": analysis.sentiment.value if analysis.sentiment else None,
                    "lead_score": analysis.lead_score,
                },
            }
        )

    record_audit_event(
        db,
        actor=actor,
        action="data.export",
        entity_type="contact",
        entity_id=contact.id,
        metadata={"attempts": len(out_attempts), "messages": message_total, "truncated": truncated},
    )
    return {
        "contact": {
            "id": str(contact.id),
            "campaign_id": str(contact.campaign_id),
            "phone_number": None if contact.purged_at else contact.phone_number,
            "status": contact.status.value,
            "attempt_count": contact.attempt_count,
            "purged_at": _iso(contact.purged_at),
            "created_at": _iso(contact.created_at),
        },
        "attempts": out_attempts,
        "truncated": truncated,
        "limits": {"max_attempts": MAX_EXPORT_ATTEMPTS, "max_messages": MAX_EXPORT_MESSAGES},
    }
