"""Shared by the dialer's reconciliation gate and the Dograh completion
webhook: what an unresolved ambiguous trigger is, and how its attempt is
reopened once a Dograh run is known to exist.

An ambiguous trigger (timeout after the request may have left) is recorded as
FAILED_TO_CONNECT with no run id. That is provisional, not final: a run can
still turn up -- via reconciliation or via the run's own completion webhook --
and the webhook refuses terminal attempts, so the attempt must be reopened or
the real call's outcome would be lost.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus

AMBIGUOUS_TRIGGER_EVENT = "DOGRAH_TRIGGER_AMBIGUOUS"


def is_unresolved_ambiguous_trigger(db: Session, attempt: CallAttempt) -> bool:
    if attempt.state != CallAttemptState.FAILED_TO_CONNECT or attempt.provider_call_id is not None:
        return False
    return (
        db.execute(
            select(CallEvent.id)
            .where(
                CallEvent.call_attempt_id == attempt.id,
                CallEvent.event_type == AMBIGUOUS_TRIGGER_EVENT,
            )
            .limit(1)
        ).first()
        is not None
    )


def reopen_for_adoption(attempt: CallAttempt, contact: Contact) -> None:
    """FAILED_TO_CONNECT -> INITIATED / DIALING. Never CONNECTED: the
    completion webhook decides what actually happened."""
    attempt.state = CallAttemptState.INITIATED
    attempt.ended_at = None
    attempt.connection_failure_reason = None
    contact.status = ContactStatus.DIALING
