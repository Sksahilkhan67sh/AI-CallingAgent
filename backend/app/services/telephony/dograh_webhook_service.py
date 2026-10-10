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
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import (
    CallAttemptState,
    ContactStatus,
    ConversationSessionStatus,
    SuppressionSource,
)
from app.models.processed_event import ProcessedEvent
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.analysis.admission import enqueue_call_analysis
from app.services.audit_service import record_audit_event
from app.services.processed_event_service import claim_event
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony.dograh_outcome import classify_call_outcome, is_opt_out
from app.services.telephony.dograh_reconciliation import (
    is_unresolved_ambiguous_trigger,
    reopen_for_adoption,
)
from app.services.telephony.transcript_fetch import fetch_transcript_lines, resolve_target

logger = logging.getLogger("dograh_webhook")

_ACTOR = "dograh-webhook"
_EVENT_TYPE = "dograh.call_completed"

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


def _suppress_for_opt_out(
    db: Session, attempt: CallAttempt, contact: Contact, payload: DograhWebhookPayload
) -> None:
    """CP13: durable, idempotent opt-out. CP14: the conflict target is the normalized NUMBER
    (global), not the contact: ON CONFLICT DO NOTHING makes N concurrent/repeated opt-outs
    -- even from two contacts sharing a number -- converge on one row with no error and
    (because only the inserting call audits) no duplicate audit trail. Written to
    PostgreSQL in the same transaction as the attempt outcome: if it fails, the webhook is
    not acknowledged and the event is not marked processed. Redis is never consulted.

    Evidence used is recorded verbatim in the audit row: the configured disposition code, not
    any transcript text and no phone number."""
    inserted = SuppressionRepository(db).insert_if_absent(
        contact.normalized_phone_number,
        source=SuppressionSource.AGENT_IN_CALL,
        reason="opt-out via Dograh call disposition",
        contact_id=contact.id,
    )
    contact.status = ContactStatus.CLOSED
    db.flush()
    if inserted is not None:
        record_audit_event(
            db,
            actor=_ACTOR,
            action="dograh.opt_out_suppressed",
            entity_type="contact",
            entity_id=contact.id,
            metadata={
                "call_attempt_id": str(attempt.id),
                "campaign_id": str(contact.campaign_id),
                "call_disposition": payload.call_disposition,
                "mapped_call_disposition": payload.mapped_call_disposition,
            },
        )
        logger.info(
            "opt_out_suppressed",
            extra={"attempt_id": str(attempt.id), "campaign_id": str(contact.campaign_id)},
        )


_MAX_DURATION_SECONDS = 24 * 3600.0


def _parse_duration(value: float | str | None) -> float | None:
    """CP14: the provider-reported call length, for the spend ESTIMATE only. Untrusted input:
    anything non-numeric, non-finite or negative is treated as unknown (None), and a huge
    value is capped at a day, so a bad payload can neither crash the webhook nor skew the
    estimate without bound."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _MAX_DURATION_SECONDS)


def _parse_positive_int(value: int | str | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _already_processed(payload: DograhWebhookPayload) -> "DograhWebhookResult":
    try:
        attempt_id = uuid.UUID(payload.call_attempt_id)
    except ValueError:
        attempt_id = uuid.UUID(int=0)
    return DograhWebhookResult(call_attempt_id=attempt_id, outcome="already_processed")


def _is_safe_transcript_url(url: str) -> bool:
    """CP10 SSRF guard, hardened in CP11 -- see transcript_fetch.py."""
    return resolve_target(url) is not None


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
    lines = fetch_transcript_lines(transcript_url)
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


def _reject_correlation(
    db: Session, payload: DograhWebhookPayload, attempt: CallAttempt, problem: str
) -> DograhWebhookError:
    """A webhook that does not belong to this attempt: audit it, mutate
    nothing, answer 409. The audit row is committed here because the request
    is about to fail and get_db rolls back on an exception."""
    record_audit_event(
        db,
        actor=_ACTOR,
        action="dograh.webhook_correlation_rejected",
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={"problem": problem, "workflow_run_id": str(payload.workflow_run_id)},
    )
    db.commit()
    logger.warning(
        "dograh_webhook_correlation_rejected",
        extra={"attempt_id": str(attempt.id), "problem": problem},
    )
    return DograhWebhookError(409, "Webhook does not correlate to this call attempt")


def _validate_correlation(
    db: Session,
    payload: DograhWebhookPayload,
    attempt: CallAttempt,
    contact: Contact,
    unresolved_ambiguous: bool,
) -> None:
    """CP10: a webhook may only touch the attempt it belongs to. Checked
    before any state change and before the terminal-attempt no-op, so a wrong
    callback is rejected and audited rather than silently accepted."""
    if attempt.provider not in (None, "dograh"):
        raise _reject_correlation(db, payload, attempt, "attempt_not_dograh")
    if payload.contact_id is not None and payload.contact_id != str(attempt.contact_id):
        raise _reject_correlation(db, payload, attempt, "contact_mismatch")
    if payload.campaign_id is not None and payload.campaign_id != str(contact.campaign_id):
        raise _reject_correlation(db, payload, attempt, "campaign_mismatch")

    if payload.workflow_run_id is None:
        return
    run_id = str(payload.workflow_run_id)
    if attempt.provider_call_id is not None and attempt.provider_call_id != run_id:
        raise _reject_correlation(db, payload, attempt, "run_mismatch")
    if attempt.provider_call_id is None or unresolved_ambiguous:
        # About to adopt this run for the attempt: it must not already
        # belong to a different attempt (uq_call_attempt_provider_call_id).
        owner = db.execute(
            select(CallAttempt.id).where(
                CallAttempt.provider_call_id == run_id, CallAttempt.id != attempt.id
            )
        ).first()
        if owner is not None:
            raise _reject_correlation(db, payload, attempt, "run_owned_by_other_attempt")


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

    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact is not None else None
    if contact is None or campaign is None:
        raise DograhWebhookError(404, "Contact or campaign for this call attempt is missing")

    _validate_correlation(db, payload, attempt, contact, unresolved_ambiguous)
    if not unresolved_ambiguous and attempt.state in (
        CallAttemptState.ENDED_NORMALLY,
        CallAttemptState.DROPPED_MID_CALL,
        CallAttemptState.FAILED_TO_CONNECT,
    ):
        # A webhook with an event id we have not seen, aimed at an attempt that already has
        # an outcome, is not a normal duplicate. It is refused and left on the record.
        logger.warning(
            "invalid_provider_transition",
            extra={"attempt_id": str(attempt.id), "from_state": attempt.state.value},
        )
        record_audit_event(
            db,
            actor=_ACTOR,
            action="dograh.invalid_provider_transition",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"from_state": attempt.state.value, "call_status": payload.call_status},
        )
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="already_processed")

    classification = classify_call_outcome(payload.call_status, payload.call_disposition)
    if classification.non_final:
        # `ringing` / `in-progress` ... on a completion webhook: the call may still be live,
        # so no decision may be taken from it. The event is deliberately NOT claimed, so the
        # real completion for the same run is still processed when it arrives.
        logger.warning(
            "dograh_webhook_non_final_status", extra={"attempt_id": str(attempt.id)}
        )
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="non_final_ignored")
    opted_out = is_opt_out(
        payload.call_disposition,
        payload.mapped_call_disposition,
        get_settings().dograh_opt_out_dispositions,
    )
    state = classification.state
    disconnect_reason = classification.mid_call_reason
    never_connected_reason = classification.never_connected_reason
    ended_at = datetime.now(UTC)

    # The unique constraint is the final authority: of N concurrent
    # identical deliveries exactly one claims the event; the rest are
    # idempotent no-ops that have mutated nothing.
    if not claim_event(db, event_id, _EVENT_TYPE):
        return DograhWebhookResult(call_attempt_id=attempt.id, outcome="already_processed")

    if (
        not unresolved_ambiguous
        and attempt.provider_call_id is None
        and payload.workflow_run_id is not None
    ):
        # The completion webhook beat the dialer's own trigger-response
        # bookkeeping: remember which run this attempt belongs to. The dialer
        # later writes the same value, so the order of the two is irrelevant.
        attempt.provider = "dograh"
        attempt.provider_call_id = str(payload.workflow_run_id)

    duration = _parse_duration(payload.duration_seconds)
    if duration is not None:
        attempt.duration_seconds = duration

    # CP14B: remember which Dograh workflow produced this run (first value wins; redelivery
    # is a no-op). Needed later to fetch the run's QA annotations.
    workflow_id = _parse_positive_int(payload.workflow_id)
    if workflow_id is not None and attempt.dograh_workflow_id is None:
        attempt.dograh_workflow_id = workflow_id

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

    if opted_out:
        _suppress_for_opt_out(db, attempt, contact, payload)

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
                "basis": classification.basis,
                "call_disposition": payload.call_disposition,
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
            reason_key=classification.reason_key or never_connected_reason.value,
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
        if not opted_out:  # an opt-out leaves the contact Closed, not Completed
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
    if not opted_out:
        contact.status = ContactStatus.DISCONNECTED
    db.flush()
    record_audit_event(
        db,
        actor=_ACTOR,
        action="call_attempt.dropped_mid_call_via_dograh_webhook",
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={
            "call_status": payload.call_status,
            "reason": disconnect_reason.value,
            "basis": classification.basis,
        },
    )
    RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=classification.reason_key or disconnect_reason.value,
    )
    return DograhWebhookResult(call_attempt_id=attempt.id, outcome="dropped_mid_call")
