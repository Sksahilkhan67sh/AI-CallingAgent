"""CP09 -- reconciliation of ambiguous triggers, lost webhooks, lost runs."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.call_attempt import CallAttempt
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.dograh_client import DograhApiError, DograhRun
from app.services.telephony.dograh_reconciler import reconcile_stale_attempts

from . import _cp09 as h


@pytest.fixture(autouse=True)
def _clean_redis(redis_client):
    yield


class FakeReadClient:
    def __init__(self, *, found=None, run=None, error=None):
        self.found, self.run, self.error = found, run, error
        self.lookups = 0

    def find_run_by_attempt(self, *, call_attempt_id, since):
        self.lookups += 1
        if self.error:
            raise self.error
        return self.found

    def get_run(self, run_id):
        self.lookups += 1
        if self.error:
            raise self.error
        return self.run


def _run(run_id=900, *, completed=True, status="user_hangup", duration=61):
    return DograhRun(
        run_id=run_id,
        is_completed=completed,
        initial_context={},
        gathered_context={"call_status": status},
        cost_info={"call_duration_seconds": duration},
        transcript_url=None,
        recording_url=None,
    )


def _stale_attempt(db, *, run_id=None, age=600, state=CallAttemptState.INITIATED):
    _, contact = h.setup_contact(db, phone="555-990-0001")
    contact.status = ContactStatus.DIALING
    contact.attempt_count = 1
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider="dograh",
        provider_call_id=run_id,
        state=state,
        started_at=datetime.now(UTC) - timedelta(seconds=age),
    )
    db.add(attempt)
    db.flush()
    db.commit()  # the reconciler rolls back on provider errors
    return contact, attempt


def test_ambiguous_attempt_adopts_the_run_dograh_actually_created(db_session):
    contact, a = _stale_attempt(db_session)
    client = FakeReadClient(found=_run(901, completed=True))

    assert reconcile_stale_attempts(db_session, client) == 1

    db_session.refresh(a)
    assert a.provider_call_id == "901"
    assert a.state == CallAttemptState.ENDED_NORMALLY
    assert "RECONCILED_RUN_ADOPTED" in [e.event_type for e in db_session.query(CallEvent)]
    assert get_recovery_scheduler().pending_count() == 0  # no duplicate call, no retry


def test_ambiguous_attempt_with_no_run_found_goes_to_recovery_not_a_blind_retry(db_session):
    contact, a = _stale_attempt(db_session)

    reconcile_stale_attempts(db_session, FakeReadClient(found=None))

    db_session.refresh(a)
    assert a.state == CallAttemptState.FAILED_TO_CONNECT
    assert get_recovery_scheduler().pending_count() == 1  # RecoveryManager decided


def test_dograh_unreachable_leaves_the_attempt_untouched(db_session):
    _, a = _stale_attempt(db_session)
    client = FakeReadClient(error=DograhApiError(503, "down"))

    assert reconcile_stale_attempts(db_session, client) == 0

    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED
    assert get_recovery_scheduler().pending_count() == 0


def test_lost_webhook_is_recovered_by_reading_the_completed_run(db_session):
    contact, a = _stale_attempt(db_session, run_id="902")
    reconcile_stale_attempts(db_session, FakeReadClient(run=_run(902)))
    db_session.refresh(a)
    db_session.refresh(contact)
    assert a.state == CallAttemptState.ENDED_NORMALLY
    assert contact.status == ContactStatus.COMPLETED


def test_lost_no_answer_is_recovered_as_failed_to_connect(db_session):
    _, a = _stale_attempt(db_session, run_id="903")
    run = _run(903, status="no-answer", duration=0)
    reconcile_stale_attempts(db_session, FakeReadClient(run=run))
    db_session.refresh(a)
    assert a.state == CallAttemptState.FAILED_TO_CONNECT


def test_call_still_in_progress_is_left_alone(db_session):
    _, a = _stale_attempt(db_session, run_id="904", age=300)
    reconcile_stale_attempts(db_session, FakeReadClient(run=_run(904, completed=False)))
    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED


def test_run_stuck_in_progress_past_the_stale_limit_is_terminalized(db_session):
    _, a = _stale_attempt(db_session, run_id="905", age=4000)
    reconcile_stale_attempts(db_session, FakeReadClient(run=_run(905, completed=False)))
    db_session.refresh(a)
    assert a.state == CallAttemptState.FAILED_TO_CONNECT


def test_recent_attempts_are_not_reconciled(db_session):
    _, a = _stale_attempt(db_session, age=5)
    client = FakeReadClient(found=None)
    assert reconcile_stale_attempts(db_session, client) == 0
    assert client.lookups == 0
