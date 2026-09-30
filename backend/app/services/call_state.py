"""Call-attempt state transitions -- Checkpoint 09.

One table, one function. Every code path that moves a CallAttempt between
states goes through `transition()`, which rejects illegal moves and writes
an auditable CallEvent. Terminal states never move again, so a duplicate
or replayed provider event can never resurrect or corrupt a finished call.

STATE (this table) is distinct from OUTCOME/REASON (connection_failure_reason,
disconnect_reason) and from ANALYSIS RESULT (CallAnalysis) -- see
Call-State-Machine.md section 5.
"""

import logging

from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState as S

logger = logging.getLogger("call_state")

TERMINAL_STATES = frozenset({S.FAILED_TO_CONNECT, S.DROPPED_MID_CALL, S.ENDED_NORMALLY})

_ALLOWED: dict[S, frozenset[S]] = {
    S.INITIATED: frozenset({S.CONNECTED, S.FAILED_TO_CONNECT}),
    S.CONNECTED: frozenset({S.ENDED_NORMALLY, S.DROPPED_MID_CALL}),
    S.FAILED_TO_CONNECT: frozenset(),
    S.DROPPED_MID_CALL: frozenset(),
    S.ENDED_NORMALLY: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, old: S, new: S) -> None:
        super().__init__(f"illegal call state transition {old.value} -> {new.value}")
        self.old = old
        self.new = new


def is_terminal(state: S) -> bool:
    return state in TERMINAL_STATES


def transition(db: Session, attempt: CallAttempt, new: S, *, reason: str, source: str) -> None:
    old = attempt.state
    if new not in _ALLOWED[old]:
        raise InvalidTransition(old, new)
    attempt.state = new
    db.add(
        CallEvent(
            call_attempt_id=attempt.id,
            event_type="CALL_STATE_TRANSITION",
            payload={"from": old.value, "to": new.value, "reason": reason, "source": source},
        )
    )
    db.flush()
    logger.info(
        "call_state_transition",
        extra={
            "attempt_id": str(attempt.id),
            "from_state": old.value,
            "to_state": new.value,
            "reason": reason,
            "source": source,
        },
    )
