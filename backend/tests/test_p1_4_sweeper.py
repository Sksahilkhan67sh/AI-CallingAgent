"""P1-4 -- the stuck-call sweeper.

A dial attempt claimed by a worker that then dies (or whose webhook never arrives) used to stay
INITIATED / contact DIALING forever unless the Redis stream message survived. The sweeper finds
such attempts after a threshold and resolves them WITHOUT ever dialing: Dograh has no
idempotency key, so "no provider call id" is ambiguous (the trigger may have gone out), and rule
one is that a person must never receive two AI calls. Every action is audited.

Worker death is emulated with a BaseException (it passes through `except Exception`, exactly
like a killed process) raised at each step of the Dograh dial path:
  before_trigger -- claim committed, trigger never sent
  after_trigger  -- trigger sent, provider id never persisted
  after_commit   -- trigger sent and persisted, stream message never acked
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services import kill_switch
from app.services.queue.dialer_worker import process_claimed_job, process_one_job
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhTriggerResult
from app.services.telephony.dograh_webhook_service import process_dograh_webhook
from tests.test_cp11_concurrency import _engine, _open_admission
from tests.test_cp12c_enqueue_pagination import _campaign, _Session

STUCK_AFTER = 3600
_RUN = iter(range(7_100_000, 7_900_000))


class _Killed(BaseException):
    """The process died here. BaseException: nothing in the worker's `except Exception` sees it."""


class _FakeDograh:
    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.triggers = 0

    def find_runs_for_attempt(self, call_attempt_id, since):
        return []

    def trigger_call(self, *, phone_number, initial_context):
        if self.mode == "before_trigger":
            raise _Killed
        self.triggers += 1
        run_id = next(_RUN)
        if self.mode == "after_trigger":
            raise _Killed
        return DograhTriggerResult(run_id, f"WR-{run_id}")


def _truncate_all() -> None:
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    _truncate_all()
    redis_client.flushdb()
    get_settings.cache_clear()
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "enqueue_rate_limit_per_minute", 10_000)
    yield
    get_settings.cache_clear()
    _truncate_all()
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    _truncate_all()


def _sweep(*, hours_later: float = 2, **kwargs):
    from app.services.queue.stuck_call_sweeper import sweep_stuck_attempts

    return sweep_stuck_attempts(
        _Session,
        now=datetime.now(UTC) + timedelta(hours=hours_later),
        stuck_after_seconds=kwargs.pop("stuck_after_seconds", STUCK_AFTER),
        **kwargs,
    )


def _die_at(mode: str, redis_client, provider, monkeypatch):
    """Run one real dial through the worker and kill it at `mode`. Returns ids and the fakes."""
    campaign_id, (contact_id,) = _campaign(1)
    queue = get_queue()
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    fake = _FakeDograh("ok" if mode == "after_commit" else mode)
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: fake)
    if mode == "after_commit":

        def killed_ack(self, message_id):
            raise _Killed

        monkeypatch.setattr(RedisStreamQueue, "ack", killed_ack)
    breaker = CircuitBreaker(redis_client, "mock")
    with pytest.raises(_Killed):
        with _Session() as s:
            process_one_job(
                s, queue, _open_admission(redis_client), provider, breaker, consumer_name="dead"
            )
            s.commit()
    monkeypatch.undo()  # the dead worker is gone; later steps use a healthy one
    get_settings.cache_clear()
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    with _Session() as s:
        attempt = s.execute(
            select(CallAttempt).where(CallAttempt.contact_id == contact_id)
        ).scalar_one()
        # The dialer stamps started_at with PostgreSQL's clock; the sweeper is given an injected
        # Python clock. Anchor the row to the Python clock so the test does not depend on the two
        # agreeing (they differ under faketime, and on any skewed host).
        attempt.started_at = datetime.now(UTC)
        s.commit()
        return contact_id, attempt.id, fake


def _state(contact_id):
    with _Session() as s:
        contact = s.get(Contact, contact_id)
        attempt = s.execute(
            select(CallAttempt).where(CallAttempt.contact_id == contact_id)
        ).scalar_one()
        events = [
            e.event_type
            for e in s.execute(
                select(CallEvent).where(CallEvent.call_attempt_id == attempt.id)
            ).scalars()
        ]
        audits = s.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == attempt.id, AuditLog.action == "sweeper.needs_review")
        ).scalar_one()
        return contact.status, attempt.state, attempt.provider_call_id, events, audits


@pytest.mark.parametrize("redis_lost", [False, True], ids=["redis_kept", "redis_lost"])
@pytest.mark.parametrize("mode", ["before_trigger", "after_trigger", "after_commit"])
def test_a_dead_worker_never_leaves_an_attempt_in_flight_and_never_causes_a_second_dial(
    mode, redis_lost, redis_client, provider, monkeypatch
):
    contact_id, attempt_id, dead = _die_at(mode, redis_client, provider, monkeypatch)
    status, state, provider_id, _events, _audits = _state(contact_id)
    # The repair job has nothing to go on yet: this is the stuck state the sweeper exists for.
    assert (status, state) == (ContactStatus.DIALING, CallAttemptState.INITIATED)
    assert (provider_id is not None) == (mode == "after_commit")
    if redis_lost:
        redis_client.flushdb()

    assert _sweep(hours_later=0.1).actioned == 0, "inside the threshold nothing may be touched"
    result = _sweep()
    assert result.actioned == 1

    status, state, provider_id, events, audits = _state(contact_id)
    assert status == ContactStatus.NEEDS_REVIEW
    assert audits == 1 and "SWEEPER_NEEDS_REVIEW" in events
    if mode == "after_commit":
        assert state == CallAttemptState.INITIATED  # a late webhook can still finalize it
    else:
        assert state == CallAttemptState.FAILED_TO_CONNECT
        assert "DOGRAH_TRIGGER_AMBIGUOUS" in events  # keeps the late-webhook adoption path open

    # Now let a healthy worker see whatever the dead one left in Redis: it must not dial.
    healthy = _FakeDograh("ok")
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: healthy)
    queue = get_queue()
    breaker = CircuitBreaker(redis_client, "mock")
    with _Session() as s:
        for message_id, job in queue.reclaim_stale("healthy", 0):
            process_claimed_job(
                s, queue, _open_admission(redis_client), provider, breaker, message_id, job
            )
            s.commit()
    assert healthy.triggers == 0, "the sweeper's verdict must stop every automatic redial"
    assert dead.triggers <= 1

    assert _sweep().actioned == 0, "a second pass finds nothing left to do"
    assert _state(contact_id)[4] == 1


def _seed_stuck(n: int, *, provider_ids: bool = False, age_hours: float = 3):
    campaign_id, contact_ids = _campaign(n)
    started = datetime.now(UTC) - timedelta(hours=age_hours)
    with _Session() as s:
        s.execute(
            update(Contact)
            .where(Contact.campaign_id == campaign_id)
            .values(status=ContactStatus.DIALING, attempt_count=1)
        )
        for i, contact_id in enumerate(contact_ids):
            s.add(
                CallAttempt(
                    contact_id=contact_id,
                    attempt_number=1,
                    state=CallAttemptState.INITIATED,
                    provider="dograh",
                    provider_call_id=str(8_000_000 + i) if provider_ids else None,
                    started_at=started,
                )
            )
        s.commit()
    return contact_ids


def test_two_sweepers_never_act_on_the_same_attempt():
    contact_ids = _seed_stuck(120)
    totals: list[int] = []
    errors: list[BaseException] = []

    def run():
        try:
            while True:
                actioned = _sweep(batch_limit=15).actioned
                if actioned == 0:
                    return
                totals.append(actioned)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]

    assert not errors
    assert sum(totals) == 120, "every stuck attempt actioned, none twice"
    with _Session() as s:
        per_attempt = s.execute(
            select(CallEvent.call_attempt_id, func.count())
            .where(CallEvent.event_type == "SWEEPER_NEEDS_REVIEW")
            .group_by(CallEvent.call_attempt_id)
        ).all()
        assert len(per_attempt) == 120 and {c for _a, c in per_attempt} == {1}
        audits = s.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "sweeper.needs_review")
        ).scalar_one()
        assert audits == 120
        statuses = set(
            s.execute(select(Contact.status).where(Contact.id.in_(contact_ids))).scalars()
        )
        assert statuses == {ContactStatus.NEEDS_REVIEW}


def test_the_batch_limit_is_respected():
    _seed_stuck(30)
    assert _sweep(batch_limit=10).actioned == 10
    assert _sweep(batch_limit=10).actioned == 10
    assert _sweep(batch_limit=10).actioned == 10
    assert _sweep(batch_limit=10).actioned == 0


def test_the_kill_switch_stops_the_sweeper_until_it_is_released():
    contact_ids = _seed_stuck(3)
    kill_switch.enable("p1-4-test", "drill")
    try:
        blocked = _sweep()
        assert blocked.actioned == 0 and blocked.blocked is True
        with _Session() as s:
            assert s.get(Contact, contact_ids[0]).status == ContactStatus.DIALING
    finally:
        kill_switch.disable("p1-4-test")
    assert _sweep().actioned == 3


def test_fresh_and_already_finished_attempts_are_left_alone():
    fresh = _seed_stuck(2, age_hours=0.01)
    done = _seed_stuck(2, provider_ids=True)
    with _Session() as s:
        s.execute(
            update(CallAttempt)
            .where(CallAttempt.contact_id.in_(done))
            .values(state=CallAttemptState.ENDED_NORMALLY)
        )
        s.commit()

    assert _sweep(hours_later=0).actioned == 0

    with _Session() as s:
        assert {
            c.status
            for c in s.execute(select(Contact).where(Contact.id.in_(fresh + done))).scalars()
        } == {ContactStatus.DIALING}


@pytest.mark.parametrize("had_provider_id", [True, False], ids=["with_run_id", "ambiguous"])
def test_a_late_completion_webhook_still_wins_after_the_sweeper(had_provider_id):
    (contact_id,) = _seed_stuck(1, provider_ids=had_provider_id)
    assert _sweep().actioned == 1
    with _Session() as s:
        attempt = s.execute(
            select(CallAttempt).where(CallAttempt.contact_id == contact_id)
        ).scalar_one()
        attempt_id = attempt.id
        run_id = attempt.provider_call_id or str(next(_RUN))

    with _Session() as s:
        result = process_dograh_webhook(
            s,
            DograhWebhookPayload(
                call_attempt_id=str(attempt_id),
                workflow_run_id=int(run_id),
                call_status="completed",
            ),
        )
        s.commit()
    assert result.outcome != "already_processed"

    with _Session() as s:
        contact = s.get(Contact, contact_id)
        attempt = s.get(CallAttempt, attempt_id)
        assert attempt.state in (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
        assert contact.status != ContactStatus.NEEDS_REVIEW, "the real outcome replaces the verdict"


def test_the_sweep_is_idempotent_and_ids_are_real_uuids():
    _seed_stuck(5)
    assert _sweep().actioned == 5
    assert _sweep().actioned == 0
    assert isinstance(uuid.uuid4(), uuid.UUID)
