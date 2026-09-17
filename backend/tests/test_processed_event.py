"""ProcessedEvent idempotency -- Webhook-Specification.md §5: the same
event_id must be rejected at the database level, not only in application
code."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.processed_event import ProcessedEvent


def test_processed_event_persists(db_session):
    event = ProcessedEvent(event_id="evt_123", event_type="call.disconnected")
    db_session.add(event)
    db_session.flush()

    assert event.id is not None


def test_duplicate_event_id_is_rejected_at_db_level(db_session):
    db_session.add(ProcessedEvent(event_id="evt_dup", event_type="call.ended"))
    db_session.flush()

    db_session.add(ProcessedEvent(event_id="evt_dup", event_type="call.ended"))
    with pytest.raises(IntegrityError):
        db_session.flush()
