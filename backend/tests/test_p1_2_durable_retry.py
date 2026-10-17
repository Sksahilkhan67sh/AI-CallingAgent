"""P1-2 -- the retry schedule lives in PostgreSQL; Redis is a rebuildable cache.

Defects reproduced here (all on the unfixed code):
  * a retry existed only as a Redis ZSET member, so FLUSHALL stranded every RETRY_SCHEDULED contact;
  * the dispatcher removed (ZREM) an entry BEFORE processing it and only logged on error, so any
    exception after the claim lost that retry for good;
  * a retry dispatched twice put two dial jobs on the stream.

CI size is small; set P1_RETRY_JOBS=1000 (the acceptance size) or more for a bigger run.
"""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, ContactStatus
from app.services.queue.factory import get_queue
from app.services.recovery import dispatch
from app.services.recovery.manager import RecoveryDecision, RecoveryManager
from app.services.recovery.scheduler import RecoveryScheduler
from tests.test_cp11_concurrency import _drain, _engine, _SpyProvider
from tests.test_cp12c_enqueue_pagination import _campaign, _Session

N_RETRIES = int(os.environ.get("P1_RETRY_JOBS", "1000"))
_KEY = "recovery:scheduled"


def _truncate_all() -> None:
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    # The rebuild scans every RETRY_SCHEDULED contact, so each test starts from empty tables.
    _truncate_all()
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    yield
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    _truncate_all()


def _schedule_retries(redis_client, n: int):
    """n contacts that each failed attempt 1, scheduled for attempt 2 by the real manager."""
    campaign_id, contact_ids = _campaign(n)
    scheduler = RecoveryScheduler(redis_client)
    with _Session() as s:
        campaign = s.get(Campaign, campaign_id)
        manager = RecoveryManager(s, scheduler)
        decision = RecoveryDecision(True, "retry", delay_seconds=30, next_attempt_number=2)
        for contact_id in contact_ids:
            contact = s.get(Contact, contact_id)
            contact.attempt_count = 1
            attempt = CallAttempt(
                contact_id=contact_id,
                attempt_number=1,
                state=CallAttemptState.FAILED_TO_CONNECT,
            )
            s.add(attempt)
            s.flush()
            manager._schedule(attempt, contact, campaign, decision)
        s.commit()
    return campaign_id, contact_ids, scheduler


def _members_by_contact(redis_client) -> dict[str, int]:
    import json

    counts: dict[str, int] = {}
    for member in redis_client.zrange(_KEY, 0, -1):
        contact = json.loads(member)["contact_id"]
        counts[contact] = counts.get(contact, 0) + 1
    return counts


def _rebuild(redis_client):
    from app.services.recovery.rebuild import rebuild_retry_schedule

    return rebuild_retry_schedule(_Session, RecoveryScheduler(redis_client))


def test_a_scheduled_retry_is_recorded_in_postgres(redis_client):
    _cid, contact_ids, _sched = _schedule_retries(redis_client, 5)
    with _Session() as s:
        for contact_id in contact_ids:
            contact = s.get(Contact, contact_id)
            assert contact.status == ContactStatus.RETRY_SCHEDULED
            assert contact.next_retry_at is not None
            assert contact.retry_attempt_number == 2
            assert contact.retry_attempt_id is not None


def test_flushall_then_rebuild_restores_every_retry_exactly_once(redis_client):
    _cid, contact_ids, _sched = _schedule_retries(redis_client, N_RETRIES)
    assert redis_client.zcard(_KEY) == N_RETRIES

    redis_client.flushall()  # Redis lost
    assert redis_client.zcard(_KEY) == 0

    _rebuild(redis_client)
    counts = _members_by_contact(redis_client)
    assert len(counts) == N_RETRIES
    assert set(counts.values()) == {1}, "a retry must be scheduled once, never twice"
    assert redis_client.zcard(_KEY) == N_RETRIES

    _rebuild(redis_client)  # idempotent: a second run adds nothing
    assert redis_client.zcard(_KEY) == N_RETRIES


def test_rebuild_does_not_duplicate_entries_that_survived(redis_client):
    _cid, _ids, _sched = _schedule_retries(redis_client, 50)
    assert redis_client.zcard(_KEY) == 50  # Redis is fine: nothing is missing

    _rebuild(redis_client)

    assert redis_client.zcard(_KEY) == 50


def test_two_concurrent_rebuilders_schedule_each_retry_once(redis_client):
    _cid, contact_ids, _sched = _schedule_retries(redis_client, 300)
    redis_client.flushall()
    errors: list[BaseException] = []

    def run():
        try:
            _rebuild(redis_client)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]

    assert not errors
    counts = _members_by_contact(redis_client)
    assert len(counts) == 300 and set(counts.values()) == {1}


def test_an_error_after_the_claim_does_not_lose_the_retry(redis_client, monkeypatch):
    _cid, _ids, scheduler = _schedule_retries(redis_client, 1)
    queue = get_queue()

    def boom(*_a, **_k):
        raise RuntimeError("db hiccup after the claim")

    monkeypatch.setattr(dispatch, "_process_one", boom)
    due = datetime.now(UTC) + timedelta(seconds=120)

    assert dispatch.dispatch_due_recovery_jobs(scheduler, queue, now=due) == 0

    assert scheduler.pending_count() == 1, "the claimed retry must be put back, never dropped"


def test_dispatching_twice_never_dials_twice(redis_client, provider):
    _cid, contact_ids, scheduler = _schedule_retries(redis_client, 1)
    queue = get_queue()
    spy = _SpyProvider(provider)
    (member,) = redis_client.zrange(_KEY, 0, -1)
    due = datetime.now(UTC) + timedelta(seconds=120)

    assert dispatch.dispatch_due_recovery_jobs(scheduler, queue, now=due) == 1
    scheduler.reschedule(member, due - timedelta(seconds=1))  # duplicate / replayed entry
    assert dispatch.dispatch_due_recovery_jobs(scheduler, queue, now=due) == 0

    assert redis_client.xlen(queue.stream_key) == 1, "one retry must be one dial job"
    _drain(spy, redis_client)
    assert spy.calls == 1
    with _Session() as s:
        numbers = sorted(
            a.attempt_number for a in s.query(CallAttempt).filter_by(contact_id=contact_ids[0])
        )
    assert numbers == [1, 2]


def test_the_postgres_schedule_is_cleared_once_the_dialer_claims_the_retry(
    redis_client, provider
):
    _cid, contact_ids, scheduler = _schedule_retries(redis_client, 1)
    queue = get_queue()
    spy = _SpyProvider(provider)
    due = datetime.now(UTC) + timedelta(seconds=120)
    dispatch.dispatch_due_recovery_jobs(scheduler, queue, now=due)
    with _Session() as s:  # dispatched but not yet dialed: still scheduled, re-check bumped
        contact = s.get(Contact, contact_ids[0])
        assert contact.status == ContactStatus.RETRY_SCHEDULED
        assert contact.next_retry_at is not None and contact.next_retry_at > due

    _drain(spy, redis_client)

    with _Session() as s:
        contact = s.get(Contact, contact_ids[0])
        assert contact.next_retry_at is None
        assert contact.retry_attempt_id is None and contact.retry_attempt_number is None


def test_uuid_helpers_are_stable():  # guards the deterministic ids the rebuild relies on
    from app.services.recovery.job import RecoveryJob

    attempt, contact, campaign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a = RecoveryJob.new(
        attempt_id=attempt, contact_id=contact, campaign_id=campaign, attempt_number=2
    )
    b = RecoveryJob.new(
        attempt_id=attempt, contact_id=contact, campaign_id=campaign, attempt_number=2
    )
    assert a.job_id == b.job_id and a.trace_id == b.trace_id
