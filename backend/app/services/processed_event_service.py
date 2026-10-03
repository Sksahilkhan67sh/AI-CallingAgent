"""Webhook idempotency claim -- Checkpoint 09 follow-up.

The ProcessedEvent unique constraint is the final authority on "has this
event already been handled". A plain check-then-insert lets two
concurrent identical deliveries both pass the check; the loser then hit
an IntegrityError (a 500) at insert time. Claiming the event *first*,
inside a SAVEPOINT, turns that conflict into an ordinary "already
processed" answer and guarantees the loser has mutated nothing.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.processed_event import ProcessedEvent


def claim_event(db: Session, event_id: str, event_type: str) -> bool:
    """Returns True if this call won the right to process `event_id`,
    False if another delivery already has (or concurrently won it).

    A concurrent winner holds the unique-index entry until its own
    transaction commits, so the loser blocks here and then gets the
    conflict -- by which point the winner's work is durable.
    """
    try:
        with db.begin_nested():
            db.add(ProcessedEvent(event_id=event_id, event_type=event_type))
            db.flush()
    except IntegrityError:
        return False
    return True
