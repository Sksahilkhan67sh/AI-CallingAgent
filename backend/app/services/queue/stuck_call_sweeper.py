"""Stuck-call sweeper (P1-4).

An attempt a worker claimed and then lost -- the worker died, the process was killed after the
trigger, Redis was wiped so the stream message is gone, or the completion webhook never arrived
-- stays INITIATED with its contact DIALING forever. The normal repair (stream reclaim) only works
while the stream message survives. This is the backstop.

Policy (rule one: a person must never receive two AI calls; a missed call is cheaper):

* It NEVER dials and never enqueues a dial. The only thing it can do to an attempt is stop
  treating it as in flight.
* No provider call id is NOT proof that no call was placed: the trigger request may have reached
  Dograh and the process died before the id was saved (Dograh has no idempotency key). So that
  case, like "an id exists but no webhook ever came", is ambiguous. Both end in NEEDS_REVIEW.
* For the no-id case the attempt becomes FAILED_TO_CONNECT with the same DOGRAH_TRIGGER_AMBIGUOUS
  marker the dialer uses for an unresolved claim, so a late completion webhook is still adopted
  (dograh_reconciliation). With an id the attempt stays INITIATED so a late webhook finalizes it
  normally. Either way the real outcome replaces the verdict.
* Every action writes a CallEvent and an audit event.
* Rows are claimed with FOR UPDATE SKIP LOCKED inside the transaction that acts on them: two
  sweepers (or a sweeper and a webhook) never act on the same row, and a crashed sweeper's locks
  disappear with its connection. Batch-limited. A kill switch or provider halt that blocks
  outbound makes the sweep a no-op (state is left as the freeze found it).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.services import kill_switch
from app.services.audit_service import record_audit_event
from app.services.telephony.dograh_reconciliation import AMBIGUOUS_TRIGGER_EVENT

logger = logging.getLogger("sweeper")

ACTOR = "stuck-call-sweeper"
EVENT_NEEDS_REVIEW = "SWEEPER_NEEDS_REVIEW"
ACTION_NEEDS_REVIEW = "sweeper.needs_review"
REASON_NO_PROVIDER_ID = "no_provider_call_id"
REASON_NO_WEBHOOK = "no_terminal_webhook"


@dataclass(frozen=True)
class SweepResult:
    actioned: int
    blocked: bool = False


def sweep_stuck_attempts(
    session_factory: Callable[[], Session],
    *,
    now: datetime | None = None,
    stuck_after_seconds: int | None = None,
    batch_limit: int | None = None,
) -> SweepResult:
    """Handle at most `batch_limit` stuck attempts. Call again until `actioned == 0`."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    threshold = stuck_after_seconds or settings.sweeper_stuck_after_seconds
    limit = batch_limit or settings.sweeper_batch_limit

    if kill_switch.block_reason() is not None:
        logger.warning("sweeper_blocked_by_outbound_control")
        return SweepResult(0, blocked=True)

    cutoff = now - timedelta(seconds=threshold)
    actioned = 0
    with session_factory() as db:
        rows = db.execute(
            select(CallAttempt, Contact)
            .join(Contact, Contact.id == CallAttempt.contact_id)
            .where(
                CallAttempt.state == CallAttemptState.INITIATED,
                CallAttempt.started_at < cutoff,
                Contact.status == ContactStatus.DIALING,
            )
            .order_by(CallAttempt.started_at, CallAttempt.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=(CallAttempt, Contact))
        ).all()
        for attempt, contact in rows:
            _mark_for_review(db, attempt, contact, now)
            actioned += 1
        db.commit()
    if actioned:
        logger.warning("sweeper_marked_for_review", extra={"count": actioned})
    return SweepResult(actioned)


def _mark_for_review(db: Session, attempt: CallAttempt, contact: Contact, now: datetime) -> None:
    age_seconds = int((now - attempt.started_at).total_seconds())
    had_provider_id = attempt.provider_call_id is not None
    reason = REASON_NO_WEBHOOK if had_provider_id else REASON_NO_PROVIDER_ID

    if not had_provider_id:
        attempt.provider = attempt.provider or "dograh"
        attempt.state = CallAttemptState.FAILED_TO_CONNECT
        attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
        attempt.ended_at = now
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type=AMBIGUOUS_TRIGGER_EVENT,
                payload={"category": "swept_stuck", "reconcile": "not_attempted"},
            )
        )
    contact.status = ContactStatus.NEEDS_REVIEW

    payload = {
        "reason": reason,
        "age_seconds": age_seconds,
        "had_provider_call_id": had_provider_id,
        "attempt_number": attempt.attempt_number,
    }
    db.add(CallEvent(call_attempt_id=attempt.id, event_type=EVENT_NEEDS_REVIEW, payload=payload))
    record_audit_event(
        db,
        actor=ACTOR,
        action=ACTION_NEEDS_REVIEW,
        entity_type="call_attempt",
        entity_id=attempt.id,
        metadata={**payload, "contact_id": str(contact.id)},
    )
    db.flush()
