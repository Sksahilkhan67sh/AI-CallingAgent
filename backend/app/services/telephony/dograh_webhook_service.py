"""Processes Dograh's post-call webhook -- Checkpoint 08.

By the time this webhook fires, the call already connected on Dograh's
side (a trigger-time failure -- bad phone number, telephony not
configured -- is a 4xx on the *trigger* request, handled synchronously
in app/services/queue/dialer_worker.py::_place_call_via_dograh, and
never reaches here). So this only ever needs to classify between
"ended normally" and "dropped mid-call", never "never connected" --
which simplifies the classification versus the native CP03/04/05 path.

Reuses the existing terminal-state machinery rather than inventing a
parallel one: an "ended normally" outcome goes through the same
`enqueue_call_analysis` CP06 already uses; a "dropped mid-call" outcome
goes through the same `RecoveryManager.handle_disconnect` CP05 already
uses -- both exactly as `ConversationOrchestrator` would have called
them for a native call, just triggered from a webhook instead of a
live conversation loop.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import (
    CallAttemptState,
    ContactStatus,
    ConversationSessionStatus,
    MidCallDisconnectReason,
)
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.analysis.admission import enqueue_call_analysis
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager

logger = logging.getLogger("dograh_webhook")

# §Classification heuristic -- Dograh's `call_status` is a free-text
# "observed reason the call ended" (its own docs: "never inferred"),
# with no fixed enum published. Rather than guess an exhaustive
# vocabulary, this keys off the same kind of keyword match already
# used elsewhere in this codebase for an equivalent problem (e.g.
# FakeAnalysisLLM's keyword-driven classification, Checkpoint 06) --
# documented as a heuristic, easy to extend, and biased toward
# "ended normally" since a call that produced a webhook at all ran a
# full workflow to completion in the large majority of cases.
_DROPPED_MID_CALL_KEYWORDS = (
    "error",
    "fail",
    "timeout",
    "disconnect",
    "drop",
    "technical",
)
_NETWORK_KEYWORDS = ("network", "connection")


class DograhWebhookError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class DograhWebhookResult:
    call_attempt_id: uuid.UUID
    outcome: str  # "already_processed" | "ended_normally" | "dropped_mid_call"


def _classify(call_status: str | None) -> tuple[CallAttemptState, MidCallDisconnectReason | None]:
    text = (call_status or "").lower()
    if any(keyword in text for keyword in _DROPPED_MID_CALL_KEYWORDS):
        reason = (
            MidCallDisconnectReason.NETWORK_PROBLEM
            if any(k in text for k in _NETWORK_KEYWORDS)
            else MidCallDisconnectReason.TECHNICAL_ISSUE
        )
        return CallAttemptState.DROPPED_MID_CALL, reason
    return CallAttemptState.ENDED_NORMALLY, None


def _fetch_transcript_lines(transcript_url: str) -> list[dict]:
    """Best-effort only -- Dograh's transcript export format isn't
    fixed in its published docs (transcript_url is just "a public
    download URL"). Never raises: a fetch/parse failure here must not
    fail webhook processing, since the call's terminal-state
    transition is the important, durable part (§ below)."""
    try:
        response = httpx.get(transcript_url, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning("dograh_transcript_fetch_failed", extra={"url": transcript_url})
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _role_for(raw_role: str) -> ConversationRole:
    text = raw_role.lower()
    if any(k in text for k in ("user", "customer", "caller", "human")):
        return ConversationRole.CONTACT
    if any(k in text for k in ("assistant", "agent", "bot", "ai")):
        return ConversationRole.AGENT
    return ConversationRole.SYSTEM


def _populate_transcript(
    db: Session, session: ConversationSession, transcript_url: str | None
) -> None:
    if not transcript_url:
        return
    lines = _fetch_transcript_lines(transcript_url)
    for i, line in enumerate(lines, start=1):
        raw_role = str(line.get("role") or line.get("speaker") or "")
        content = str(line.get("content") or line.get("text") or line.get("message") or "").strip()
        if not content:
            continue
        db.add(
            ConversationMessage(
                session_id=session.id,
                sequence=i,
                role=_role_for(raw_role),
                content=content,
            )
        )


def process_dograh_webhook(db: Session, payload: DograhWebhookPayload) -> DograhWebhookResult:
    try:
        attempt_id = uuid.UUID(payload.call_attempt_id)
    except ValueError as exc:
        raise DograhWebhookError(422, "call_attempt_id is not a valid UUID") from exc

    attempt = db.get(CallAttempt, attempt_id)
    if attempt is None:
        raise DograhWebhookError(404, f"No call attempt {attempt_id}")

    # Idempotent redelivery (Dograh's own docs: "Handle duplicate
    # deliveries idempotently -- retries may deliver the same payload
    # more than once"). A terminal attempt is never reprocessed.
    if attempt.state in (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL):
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="already_processed")

    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact is not None else None
    if contact is None or campaign is None:
        raise DograhWebhookError(404, "Contact or campaign for this call attempt is missing")

    session = ConversationSession(
        call_attempt_id=attempt.id,
        status=ConversationSessionStatus.ENDED,
        ended_at=datetime.now(UTC),
    )
    db.add(session)
    db.flush()
    _populate_transcript(db, session, payload.transcript_url)

    state, disconnect_reason = _classify(payload.call_status)
    ended_at = datetime.now(UTC)

    if state == CallAttemptState.ENDED_NORMALLY:
        attempt.state = CallAttemptState.ENDED_NORMALLY
        attempt.ended_at = ended_at
        contact.status = ContactStatus.COMPLETED
        db.flush()
        enqueue_call_analysis(db, attempt, contact)
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="ended_normally")

    assert disconnect_reason is not None
    attempt.state = CallAttemptState.DROPPED_MID_CALL
    attempt.disconnect_reason = disconnect_reason
    attempt.ended_at = ended_at
    contact.status = ContactStatus.DISCONNECTED
    db.flush()
    RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
        attempt, contact, campaign, never_connected=False, reason_key=disconnect_reason.value
    )
    return DograhWebhookResult(call_attempt_id=attempt.id, outcome="dropped_mid_call")
