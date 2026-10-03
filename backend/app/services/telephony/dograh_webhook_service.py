"""Processes Dograh's post-call webhook -- Checkpoint 08, hardened in
Checkpoint 09.

Reuses the existing terminal-state machinery rather than inventing a
parallel one: an "ended normally" outcome goes through the same
`enqueue_call_analysis` CP06 already uses; a "dropped mid-call" or
"never connected" outcome goes through the same
`RecoveryManager.handle_disconnect` CP05 already uses -- both exactly
as the native path would have called them, just triggered from a
webhook instead of a live conversation loop or a trigger-time HTTP
failure.

Checkpoint 09 §2 correction from Checkpoint 08: a successful *trigger*
only ever meant Dograh accepted the job (see
app/services/queue/dialer_worker.py::_place_call_via_dograh) --
`CallAttempt.state` stays at its default INITIATED until this webhook
arrives. That means this module, not the trigger path, is the only
place that ever learns whether a Dograh-routed call actually connected
at all -- so classification here is three-way (never connected /
dropped mid-call / ended normally), not the two-way split Checkpoint
08 shipped with.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
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
    NeverConnectedFailureReason,
)
from app.models.processed_event import ProcessedEvent
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.analysis.admission import enqueue_call_analysis
from app.services.audit_service import record_audit_event
from app.services.processed_event_service import claim_event
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony.dograh_reconciliation import (
    is_unresolved_ambiguous_trigger,
    reopen_for_adoption,
)

logger = logging.getLogger("dograh_webhook")

_ACTOR = "dograh-webhook"
_EVENT_TYPE = "dograh.call_completed"

# §Classification heuristic -- Dograh's `call_status` is a free-text
# "observed reason the call ended" (its own docs: "never inferred"),
# with no fixed enum published. Rather than guess an exhaustive
# vocabulary, this keys off the same kind of keyword match already
# used elsewhere in this codebase for an equivalent problem (e.g.
# FakeAnalysisLLM's keyword-driven classification, Checkpoint 06) --
# documented as a heuristic, easy to extend.
#
# Checked in order: NEVER_CONNECTED first (most specific -- a call
# that never reached a person), then DROPPED_MID_CALL (a technical
# failure during an active conversation), else ENDED_NORMALLY. A bare
# "fail" is deliberately NOT a DROPPED_MID_CALL keyword on its own --
# it's too ambiguous between "never connected" and "dropped mid-call"
# to classify without more context, so only the more specific
# technical-failure words below trigger that bucket.
_NEVER_CONNECTED_KEYWORDS: tuple[tuple[str, NeverConnectedFailureReason], ...] = (
    ("no_answer", NeverConnectedFailureReason.NO_ANSWER),
    ("no answer", NeverConnectedFailureReason.NO_ANSWER),
    ("busy", NeverConnectedFailureReason.BUSY),
    ("invalid_number", NeverConnectedFailureReason.INVALID_NUMBER),
    ("invalid number", NeverConnectedFailureReason.INVALID_NUMBER),
    ("rejected", NeverConnectedFailureReason.REJECTED),
    ("unreachable", NeverConnectedFailureReason.NETWORK_ERROR),
)
_DROPPED_MID_CALL_KEYWORDS = ("error", "timeout", "disconnect", "drop", "technical")
_NETWORK_KEYWORDS = ("network", "connection")
# Normal completion is matched on WHOLE tokens (split on non-alphanumerics),
# never substrings -- "complete" must not match "incomplete". "user_hangup"
# (the value this integration's tests and CP08 notes assume) matches via
# the "hangup" token. Dograh publishes no enum, so this allowlist is
# UNVERIFIED against a live instance -- anything not matched here is
# treated as *unrecognized*, never as a connected conversation.
_NORMAL_COMPLETION_TOKENS = frozenset(
    {"hangup", "completed", "complete", "finished", "success", "successful"}
)


class DograhWebhookError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class DograhWebhookResult:
    call_attempt_id: uuid.UUID
    # "already_processed" | "ended_normally" | "dropped_mid_call" | "never_connected"
    outcome: str


@dataclass(frozen=True)
class _Classification:
    state: CallAttemptState
    mid_call_reason: MidCallDisconnectReason | None = None
    never_connected_reason: NeverConnectedFailureReason | None = None
    recognized: bool = True


def _classify(call_status: str | None) -> _Classification:
    text = (call_status or "").lower()

    for keyword, reason in _NEVER_CONNECTED_KEYWORDS:
        if keyword in text:
            return _Classification(
                CallAttemptState.FAILED_TO_CONNECT, never_connected_reason=reason
            )

    if any(keyword in text for keyword in _DROPPED_MID_CALL_KEYWORDS):
        mid_call_reason = (
            MidCallDisconnectReason.NETWORK_PROBLEM
            if any(k in text for k in _NETWORK_KEYWORDS)
            else MidCallDisconnectReason.TECHNICAL_ISSUE
        )
        return _Classification(CallAttemptState.DROPPED_MID_CALL, mid_call_reason=mid_call_reason)

    if _NORMAL_COMPLETION_TOKENS.intersection(re.split(r"[^a-z0-9]+", text)):
        return _Classification(CallAttemptState.ENDED_NORMALLY)

    # Unrecognized (including empty/None): we cannot claim the call
    # connected, and we cannot skip recovery for what may have been a
    # failure. Treated as a never-connected provider error -- no
    # conversation session, no analysis -- so the ONLY consequence is the
    # normal RecoveryManager decision (bounded retries, suppression and
    # eligibility re-checked). Audited loudly so operators extend the
    # keyword lists above.
    return _Classification(
        CallAttemptState.FAILED_TO_CONNECT,
        never_connected_reason=NeverConnectedFailureReason.PROVIDER_ERROR,
        recognized=False,
    )


def _already_processed(payload: DograhWebhookPayload) -> "DograhWebhookResult":
    try:
        attempt_id = uuid.UUID(payload.call_attempt_id)
    except ValueError:
        attempt_id = uuid.UUID(int=0)
    return DograhWebhookResult(call_attempt_id=attempt_id, outcome="already_processed")


def _fetch_transcript_lines(transcript_url: str) -> list[dict]:
    """Best-effort only -- Dograh's transcript export format isn't
    fixed in its published docs (transcript_url is just "a public
    download URL"). Never raises: a fetch/parse failure here must not
    fail webhook processing -- the call's terminal-state transition is
    the important, durable part."""
    try:
        response = httpx.get(transcript_url, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning("dograh_transcript_fetch_failed")
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
        content = str(
            line.get("content") or line.get("text") or line.get("message") or ""
        ).strip()
        if not content:
            continue
        db.add(
            ConversationMessage(
                session_id=session.id, sequence=i, role=_role_for(raw_role), content=content
            )
        )


def process_dograh_webhook(db: Session, payload: DograhWebhookPayload) -> DograhWebhookResult:
    # Checkpoint 09 §3.3/§3.4: replay protection + idempotency via the
    # same ProcessedEvent model/idiom the existing telephony webhook
    # already uses (app/services/webhook_service.py) -- reused, not
    # duplicated. Dograh doesn't publish a dedicated webhook event ID,
    # so workflow_run_id (unique per triggered call) is the event
    # identity; call_attempt_id is the documented fallback for the
    # rare case a run ID wasn't captured.
    event_id = f"dograh:{payload.workflow_run_id or payload.call_attempt_id}"
    if db.execute(
        select(ProcessedEvent.id).where(ProcessedEvent.event_id == event_id)
    ).first() is not None:
        return _already_processed(payload)

    try:
        attempt_id = uuid.UUID(payload.call_attempt_id)
    except ValueError as exc:
        raise DograhWebhookError(422, "call_attempt_id is not a valid UUID") from exc

    attempt = db.get(CallAttempt, attempt_id)
    if attempt is None:
        raise DograhWebhookError(404, f"No call attempt {attempt_id}")

    # Defense in depth alongside the ProcessedEvent check above: even a
    # webhook delivery with a *different* event_id must never reopen an
    # already-terminal attempt (Checkpoint 09 §2: no terminal -> active
    # transitions).
    # The one exception is an unresolved ambiguous trigger: its
    # FAILED_TO_CONNECT is provisional (no run id was ever recorded), and a
    # completion webhook is proof the run exists -- dropping it would lose the
    # real call's outcome.
    unresolved_ambiguous = is_unresolved_ambiguous_trigger(db, attempt)
    if not unresolved_ambiguous and attempt.state in (
        CallAttemptState.ENDED_NORMALLY,
        CallAttemptState.DROPPED_MID_CALL,
        CallAttemptState.FAILED_TO_CONNECT,
    ):
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="already_processed")

    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact is not None else None
    if contact is None or campaign is None:
        raise DograhWebhookError(404, "Contact or campaign for this call attempt is missing")

    classification = _classify(payload.call_status)
    state = classification.state
    disconnect_reason = classification.mid_call_reason
    never_connected_reason = classification.never_connected_reason
    ended_at = datetime.now(UTC)

    # The unique constraint is the final authority: of N concurrent
    # identical deliveries exactly one claims the event; the rest are
    # idempotent no-ops that have mutated nothing.
    if not claim_event(db, event_id, _EVENT_TYPE):
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="already_processed")

    if unresolved_ambiguous:
        reopen_for_adoption(attempt, contact)
        if payload.workflow_run_id is not None:
            attempt.provider = "dograh"
            attempt.provider_call_id = str(payload.workflow_run_id)
        record_audit_event(
            db,
            actor=_ACTOR,
            action="dograh.reconciliation_run_adopted",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"stage": "webhook", "workflow_run_id": payload.workflow_run_id},
        )
        db.flush()

    if state == CallAttemptState.FAILED_TO_CONNECT:
        assert never_connected_reason is not None
        attempt.state = CallAttemptState.FAILED_TO_CONNECT
        attempt.connection_failure_reason = never_connected_reason
        attempt.ended_at = ended_at
        db.flush()
        record_audit_event(
            db,
            actor=_ACTOR,
            action="call_attempt.never_connected_via_dograh_webhook",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={
                "call_status": payload.call_status,
                "reason": never_connected_reason.value,
                "classification": "recognized" if classification.recognized else "unrecognized",
            },
        )
        if not classification.recognized:
            logger.warning(
                "dograh_call_status_unrecognized",
                extra={"attempt_id": str(attempt.id), "status_len": len(payload.call_status or "")},
            )
        RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
            attempt,
            contact,
            campaign,
            never_connected=True,
            reason_key=never_connected_reason.value,
        )
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="never_connected")

    # From here on the call did connect -- create the conversation
    # session/transcript exactly as the native path's orchestrator
    # would have, incrementally, over the course of a real call.
    session = ConversationSession(
        call_attempt_id=attempt.id, status=ConversationSessionStatus.ENDED, ended_at=ended_at
    )
    db.add(session)
    db.flush()
    _populate_transcript(db, session, payload.transcript_url)

    if state == CallAttemptState.ENDED_NORMALLY:
        attempt.state = CallAttemptState.ENDED_NORMALLY
        attempt.ended_at = ended_at
        contact.status = ContactStatus.COMPLETED
        db.flush()
        record_audit_event(
            db,
            actor=_ACTOR,
            action="call_attempt.ended_normally_via_dograh_webhook",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"call_disposition": payload.call_disposition},
        )
        enqueue_call_analysis(db, attempt, contact)
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="ended_normally")

    assert disconnect_reason is not None
    attempt.state = CallAttemptState.DROPPED_MID_CALL
    attempt.disconnect_reason = disconnect_reason
    attempt.ended_at = ended_at
    contact.status = ContactStatus.DISCONNECTED
    db.flush()
    record_audit_event(
        db,
        actor=_ACTOR,
        action="call_attempt.dropped_mid_call_via_dograh_webhook",
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={"call_status": payload.call_status, "reason": disconnect_reason.value},
    )
    RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
        attempt, contact, campaign, never_connected=False, reason_key=disconnect_reason.value
    )
    return DograhWebhookResult(call_attempt_id=attempt.id, outcome="dropped_mid_call")
