"""Call attempts list/detail for the admin dashboard -- Checkpoint 07
§15-17, §21. No CallAttempt API existed before this checkpoint (only
internal creation by the dialer/webhooks) -- this is new, but reuses
the existing models directly rather than adding a parallel service
layer for what is fundamentally a read-only projection.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent, ConversationMessage, ConversationSession
from app.models.enums import AnalysisStatus, CallAttemptState
from app.schemas.admin import (
    CallAttemptAnalysis,
    CallAttemptDetail,
    CallAttemptListItem,
    RecoveryEvent,
    TranscriptLine,
)
from app.services.phone import mask_phone_number

# CallEvent event_type values that represent recovery/retry history
# (Checkpoint 05), as opposed to conversation-lifecycle or analysis
# events -- kept as a prefix check so a new recovery event type doesn't
# require touching this list.
_RECOVERY_EVENT_PREFIXES = ("RECOVERY_", "RETRY_")


def list_call_attempts(
    db: Session,
    *,
    campaign_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    state: CallAttemptState | None,
    analysis_status: AnalysisStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[CallAttemptListItem], int]:
    query = select(CallAttempt, Contact.campaign_id, Contact.normalized_phone_number).join(
        Contact, CallAttempt.contact_id == Contact.id
    )

    if campaign_id is not None:
        query = query.where(Contact.campaign_id == campaign_id)
    if contact_id is not None:
        query = query.where(CallAttempt.contact_id == contact_id)
    if state is not None:
        query = query.where(CallAttempt.state == state)
    if analysis_status is not None:
        query = query.join(
            CallAnalysis, CallAnalysis.call_attempt_id == CallAttempt.id
        ).where(CallAnalysis.status == analysis_status)

    total = len(db.execute(query.with_only_columns(CallAttempt.id)).all())

    rows = db.execute(
        query.order_by(CallAttempt.started_at.desc()).limit(limit).offset(offset)
    ).all()

    attempt_ids = [attempt.id for attempt, _campaign_id, _phone in rows]
    analyses = {
        a.call_attempt_id: a
        for a in db.execute(
            select(CallAnalysis).where(CallAnalysis.call_attempt_id.in_(attempt_ids))
        )
        .scalars()
        .all()
    }
    campaign_ids = {row_campaign_id for _attempt, row_campaign_id, _phone in rows}
    campaign_names = {
        c.id: c.name
        for c in db.execute(select(Campaign).where(Campaign.id.in_(campaign_ids))).scalars().all()
    }

    items = [
        CallAttemptListItem(
            id=attempt.id,
            contact_id=attempt.contact_id,
            contact_phone_masked=mask_phone_number(phone),
            campaign_id=attempt_campaign_id,
            campaign_name=campaign_names.get(attempt_campaign_id, ""),
            attempt_number=attempt.attempt_number,
            state=attempt.state,
            disconnect_reason=attempt.disconnect_reason,
            connection_failure_reason=attempt.connection_failure_reason,
            provider=attempt.provider,
            started_at=attempt.started_at,
            ended_at=attempt.ended_at,
            analysis_status=analyses[attempt.id].status if attempt.id in analyses else None,
            lead_score=analyses[attempt.id].lead_score if attempt.id in analyses else None,
        )
        for attempt, attempt_campaign_id, phone in rows
    ]
    return items, total


def get_call_attempt_detail(db: Session, call_attempt_id: uuid.UUID) -> CallAttemptDetail:
    attempt = db.get(CallAttempt, call_attempt_id)
    if attempt is None:
        raise NotFoundError(f"Call attempt {call_attempt_id} not found")

    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact else None

    session = (
        db.execute(
            select(ConversationSession).where(
                ConversationSession.call_attempt_id == attempt.id
            )
        )
        .scalars()
        .one_or_none()
    )
    transcript: list[TranscriptLine] = []
    if session is not None:
        messages = (
            db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session.id)
                .order_by(ConversationMessage.sequence)
            )
            .scalars()
            .all()
        )
        transcript = [
            TranscriptLine(role=m.role, content=m.content, created_at=m.created_at)
            for m in messages
        ]

    analysis_row = (
        db.execute(select(CallAnalysis).where(CallAnalysis.call_attempt_id == attempt.id))
        .scalars()
        .one_or_none()
    )
    analysis = (
        CallAttemptAnalysis.model_validate(analysis_row) if analysis_row is not None else None
    )

    events = (
        db.execute(
            select(CallEvent)
            .where(CallEvent.call_attempt_id == attempt.id)
            .order_by(CallEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    recovery_events = [
        RecoveryEvent(event_type=e.event_type, payload=e.payload, occurred_at=e.occurred_at)
        for e in events
        if e.event_type.startswith(_RECOVERY_EVENT_PREFIXES)
    ]

    return CallAttemptDetail(
        id=attempt.id,
        contact_id=attempt.contact_id,
        contact_phone_masked=(
            mask_phone_number(contact.normalized_phone_number) if contact else ""
        ),
        campaign_id=campaign.id if campaign else uuid.UUID(int=0),
        campaign_name=campaign.name if campaign else "",
        attempt_number=attempt.attempt_number,
        state=attempt.state,
        disconnect_reason=attempt.disconnect_reason,
        connection_failure_reason=attempt.connection_failure_reason,
        provider=attempt.provider,
        provider_call_id=attempt.provider_call_id,
        started_at=attempt.started_at,
        ended_at=attempt.ended_at,
        transcript=transcript,
        analysis=analysis,
        recovery_events=recovery_events,
    )
