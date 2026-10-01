"""Applies a Dograh call completion to our state -- Checkpoints 08, 09.

`apply_completion` is the ONE place a Dograh run's end is turned into our
state, used both by the webhook and by the reconciler (which polls Dograh
when a webhook never arrives). Retry/reconnect decisions are not made here:
failed and dropped calls are handed to the existing RecoveryManager.

Verified Dograh facts this relies on (dograh-hq/dograh docs/source):
  - `call_status` is free text ("observed reason the call ended"), no enum.
  - there is NO mid-call "connected" event; we learn about a call only when
    it completes (webhook) or by reading the run (is_completed).
So a call is never marked CONNECTED on trigger acceptance. When a
completion shows a conversation took place, the attempt passes through
CONNECTED to its terminal state as two audited transitions.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import (
    CallAttemptState,
    ContactStatus,
    ConversationSessionStatus,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
    SuppressionSource,
)
from app.models.processed_event import ProcessedEvent
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services import call_state
from app.services.analysis.admission import enqueue_call_analysis
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony import safe_fetch

logger = logging.getLogger("dograh_webhook")

EVENT_TYPE = "dograh.call_completed"

# Dispositions a workflow can emit to signal the customer asked not to be
# called again. DOGRAH-SIDE configuration: the workflow must emit one of
# these (docs/CHECKPOINT-09-NOTES.md).
_OPT_OUT_DISPOSITIONS = frozenset({"dnc", "do_not_call", "opt_out", "optout", "stop"})

_NEVER_CONNECTED_KEYWORDS: tuple[tuple[tuple[str, ...], NeverConnectedFailureReason], ...] = (
    (("busy",), NeverConnectedFailureReason.BUSY),
    (
        ("no-answer", "no_answer", "noanswer", "no answer", "unanswered"),
        NeverConnectedFailureReason.NO_ANSWER,
    ),
    (("reject", "declin"), NeverConnectedFailureReason.REJECTED),
    (("invalid", "unallocated", "not_found"), NeverConnectedFailureReason.INVALID_NUMBER),
)
_DROPPED_KEYWORDS = ("error", "fail", "timeout", "disconnect", "drop", "technical")
_NETWORK_KEYWORDS = ("network", "connection")


class DograhWebhookError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Completion:
    """Provider-neutral view of how a Dograh run ended."""

    run_id: str | None
    call_status: str | None
    disposition: str | None
    duration_seconds: float | None
    transcript_url: str | None


@dataclass
class DograhWebhookResult:
    call_attempt_id: uuid.UUID
    outcome: str  # already_processed | ended_normally | dropped_mid_call |
    #               failed_to_connect | opted_out
    needs_analysis: bool = False


def classify(
    c: Completion,
) -> tuple[CallAttemptState, NeverConnectedFailureReason | MidCallDisconnectReason | None]:
    text = (c.call_status or "").lower()
    connected = c.duration_seconds is not None and c.duration_seconds > 0
    if not connected:
        for words, reason in _NEVER_CONNECTED_KEYWORDS:
            if any(w in text for w in words):
                return CallAttemptState.FAILED_TO_CONNECT, reason
        if c.duration_seconds == 0 and any(k in text for k in _DROPPED_KEYWORDS):
            return CallAttemptState.FAILED_TO_CONNECT, NeverConnectedFailureReason.PROVIDER_ERROR
    if any(k in text for k in _DROPPED_KEYWORDS):
        dropped = (
            MidCallDisconnectReason.NETWORK_PROBLEM
            if any(k in text for k in _NETWORK_KEYWORDS)
            else MidCallDisconnectReason.TECHNICAL_ISSUE
        )
        return CallAttemptState.DROPPED_MID_CALL, dropped
    return CallAttemptState.ENDED_NORMALLY, None


def _role_for(raw_role: str) -> ConversationRole:
    text = raw_role.lower()
    if any(k in text for k in ("user", "customer", "caller", "human")):
        return ConversationRole.CONTACT
    if any(k in text for k in ("assistant", "agent", "bot", "ai")):
        return ConversationRole.AGENT
    return ConversationRole.SYSTEM


def _populate_transcript(db: Session, session: ConversationSession, url: str | None) -> None:
    """Best-effort: never fails processing, never logs the URL."""
    if not url:
        return
    try:
        data = safe_fetch.fetch_json(url, get_settings().dograh_transcript_allowed_hosts)
    except Exception as exc:
        logger.warning("dograh_transcript_fetch_failed", extra={"reason": type(exc).__name__})
        return
    if not isinstance(data, list):
        return
    sequence = 0
    for line in data:
        if not isinstance(line, dict):
            continue
        raw_role = str(line.get("role") or line.get("speaker") or "")
        content = str(line.get("content") or line.get("text") or line.get("message") or "").strip()
        if not content:
            continue
        sequence += 1
        db.add(
            ConversationMessage(
                session_id=session.id,
                sequence=sequence,
                role=_role_for(raw_role),
                content=content[:10_000],
            )
        )


def _ensure_connected(
    db: Session, attempt: CallAttempt, *, reason: str, source: str
) -> None:
    if attempt.state == CallAttemptState.INITIATED:
        call_state.transition(db, attempt, CallAttemptState.CONNECTED, reason=reason, source=source)
        metrics.incr(metrics.CALLS_CONNECTED)


def apply_completion(
    db: Session,
    attempt: CallAttempt,
    contact: Contact,
    campaign: Campaign,
    completion: Completion,
    *,
    source: str,
) -> DograhWebhookResult:
    """Caller guarantees `attempt` is not terminal and holds a row lock."""
    now = datetime.now(UTC)
    if completion.run_id and attempt.provider_call_id is None:
        attempt.provider_call_id = completion.run_id

    if (completion.disposition or "").strip().lower() in _OPT_OUT_DISPOSITIONS:
        suppressions = SuppressionRepository(db)
        if not suppressions.is_suppressed(contact.normalized_phone_number):
            suppressions.add(
                Suppression(
                    contact_id=contact.id,
                    phone_number=contact.normalized_phone_number,
                    reason="opt-out during Dograh call",
                    source=SuppressionSource.AGENT_IN_CALL,
                )
            )
        _ensure_connected(db, attempt, reason="opt_out", source=source)
        call_state.transition(
            db, attempt, CallAttemptState.ENDED_NORMALLY, reason="opt_out", source=source
        )
        attempt.ended_at = now
        contact.status = ContactStatus.CLOSED  # no retry, no analysis
        metrics.incr(metrics.OPT_OUT_COUNT)
        return DograhWebhookResult(attempt.id, "opted_out")

    state, reason = classify(completion)
    if state == CallAttemptState.FAILED_TO_CONNECT and attempt.state == CallAttemptState.CONNECTED:
        # Already verified connected: a later "failure" is a drop, not a no-connect.
        state, reason = CallAttemptState.DROPPED_MID_CALL, MidCallDisconnectReason.TECHNICAL_ISSUE

    if state == CallAttemptState.FAILED_TO_CONNECT:
        assert isinstance(reason, NeverConnectedFailureReason)
        call_state.transition(
            db, attempt, CallAttemptState.FAILED_TO_CONNECT, reason=reason.value, source=source
        )
        attempt.connection_failure_reason = reason
        attempt.ended_at = now
        metrics.incr(metrics.CALLS_FAILED)
        RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
            attempt, contact, campaign, never_connected=True, reason_key=reason.value
        )
        return DograhWebhookResult(attempt.id, "failed_to_connect")

    # A conversation took place: record the verified connection first.
    _ensure_connected(db, attempt, reason="completion_shows_conversation", source=source)
    session = ConversationSession(
        call_attempt_id=attempt.id, status=ConversationSessionStatus.ENDED, ended_at=now
    )
    db.add(session)
    db.flush()
    _populate_transcript(db, session, completion.transcript_url)

    if state == CallAttemptState.DROPPED_MID_CALL:
        assert isinstance(reason, MidCallDisconnectReason)
        call_state.transition(
            db, attempt, CallAttemptState.DROPPED_MID_CALL, reason=reason.value, source=source
        )
        attempt.disconnect_reason = reason
        attempt.ended_at = now
        contact.status = ContactStatus.DISCONNECTED
        db.flush()
        metrics.incr(metrics.CALLS_PARTIAL)
        RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
            attempt, contact, campaign, never_connected=False, reason_key=reason.value
        )
        return DograhWebhookResult(attempt.id, "dropped_mid_call")

    call_state.transition(
        db, attempt, CallAttemptState.ENDED_NORMALLY, reason="ended_normally", source=source
    )
    attempt.ended_at = now
    contact.status = ContactStatus.COMPLETED
    db.flush()
    metrics.incr(metrics.CALLS_COMPLETED)
    return DograhWebhookResult(attempt.id, "ended_normally", needs_analysis=True)


def lock_attempt(db: Session, attempt_id: uuid.UUID) -> CallAttempt | None:
    """Row lock so concurrent duplicate deliveries serialize."""
    return db.execute(
        select(CallAttempt).where(CallAttempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()


def process_dograh_webhook(db: Session, payload: DograhWebhookPayload) -> DograhWebhookResult:
    attempt = lock_attempt(db, payload.call_attempt_id)
    if attempt is None:
        raise DograhWebhookError(404, f"No call attempt {payload.call_attempt_id}")
    if attempt.provider != "dograh":
        raise DograhWebhookError(409, "Call attempt does not belong to Dograh")

    run_id = payload.run_id_str()
    if run_id is not None and attempt.provider_call_id not in (None, run_id):
        logger.warning("dograh_run_id_mismatch", extra={"attempt_id": str(attempt.id)})
        raise DograhWebhookError(409, "workflow_run_id does not match this call attempt")

    if call_state.is_terminal(attempt.state):
        metrics.incr(metrics.WEBHOOK_DUPLICATES)
        return DograhWebhookResult(attempt.id, "already_processed")

    # Replay/duplicate guard. The unique constraint on event_id is the
    # authority; the savepoint keeps a conflict from poisoning the session.
    try:
        with db.begin_nested():
            db.add(ProcessedEvent(event_id=f"dograh:{attempt.id}", event_type=EVENT_TYPE))
            db.flush()
    except IntegrityError:
        metrics.incr(metrics.WEBHOOK_DUPLICATES)
        return DograhWebhookResult(attempt.id, "already_processed")

    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact is not None else None
    if contact is None or campaign is None:
        raise DograhWebhookError(404, "Contact or campaign for this call attempt is missing")

    return apply_completion(
        db,
        attempt,
        contact,
        campaign,
        Completion(
            run_id=run_id,
            call_status=payload.call_status,
            disposition=payload.mapped_call_disposition or payload.call_disposition,
            duration_seconds=payload.duration_seconds,
            transcript_url=payload.transcript_url,
        ),
        source="dograh_webhook",
    )


def admit_analysis(db: Session, attempt_id: uuid.UUID) -> None:
    """Post-commit downstream job. The call is already durably recorded, so
    a failure here must not fail the ACK; the reconciler re-admits any
    completed call that has no analysis row."""
    try:
        attempt = db.get(CallAttempt, attempt_id)
        contact = db.get(Contact, attempt.contact_id) if attempt is not None else None
        if attempt is not None and contact is not None:
            enqueue_call_analysis(db, attempt, contact)
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("dograh_analysis_admission_failed", extra={"attempt_id": str(attempt_id)})
