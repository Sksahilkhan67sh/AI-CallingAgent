"""P1-9 (group A) -- a stale recovery entry must not be dispatched.

`dispatch._process_one` never looked at the contact's status. A recovery entry whose contact had
since moved on -- completed by a LATE webhook of the earlier attempt, parked as NEEDS_REVIEW by
the P1-4 sweeper or the P1-5 dead-letter handler, or erased -- was still dispatched: it put a dial
job on the stream and wrote RECONNECT_STARTED and a `recovery.dispatched` audit event, claiming a
redial that the dialer's eligibility check then (correctly) refused. The dialer already stopped
the call, so no one was dialed twice; the record and the queue were wrong. The dispatcher now
drops the stale entry and says why.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import ContactStatus
from app.services.queue.factory import get_queue
from app.services.recovery import dispatch
from tests.test_cp12c_enqueue_pagination import _Session
from tests.test_p1_2_durable_retry import (
    _fresh_state,  # noqa: F401  (autouse fixture)
    _schedule_retries,
    _truncate_after_module,  # noqa: F401  (autouse fixture)
)


def _dispatch(redis_client, scheduler):
    due = datetime.now(UTC) + timedelta(seconds=120)
    return dispatch.dispatch_due_recovery_jobs(scheduler, get_queue(), now=due)


def _counts():
    with _Session() as s:
        events = s.execute(
            select(func.count())
            .select_from(CallEvent)
            .where(CallEvent.event_type == "RECONNECT_STARTED")
        ).scalar_one()
        audits = s.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "recovery.dispatched")
        ).scalar_one()
    return events, audits


def test_a_retry_scheduled_contact_is_still_dispatched(redis_client):
    _cid, _ids, scheduler = _schedule_retries(redis_client, 1)
    assert _dispatch(redis_client, scheduler) == 1
    assert redis_client.xlen(get_queue().stream_key) == 1
    assert _counts() == (1, 1)


@pytest.mark.parametrize(
    "status",
    [ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL, ContactStatus.NEEDS_REVIEW],
    ids=["completed_by_late_webhook", "completed_partial", "parked_needs_review"],
)
def test_a_stale_entry_for_a_contact_that_moved_on_is_dropped_not_dispatched(redis_client, status):
    _cid, (contact_id,), scheduler = _schedule_retries(redis_client, 1)
    with _Session() as s:
        s.get(Contact, contact_id).status = status
        s.commit()

    assert _dispatch(redis_client, scheduler) == 0

    assert redis_client.xlen(get_queue().stream_key) == 0, "no dial job for a contact that moved on"
    assert scheduler.pending_count() == 0, "the stale entry is dropped, not rescheduled"
    assert _counts() == (0, 0), "no record of a redial that never happened"
    with _Session() as s:
        contact = s.get(Contact, contact_id)
        assert contact.status == status, "the dispatcher never overwrites a newer status"
        assert contact.next_retry_at is None and contact.retry_attempt_number is None


def test_a_stale_entry_for_an_erased_contact_is_dropped(redis_client):
    _cid, (contact_id,), scheduler = _schedule_retries(redis_client, 1)
    with _Session() as s:
        s.get(Contact, contact_id).purged_at = datetime.now(UTC)
        s.commit()

    assert _dispatch(redis_client, scheduler) == 0

    assert redis_client.xlen(get_queue().stream_key) == 0
    assert scheduler.pending_count() == 0
    assert _counts() == (0, 0)
