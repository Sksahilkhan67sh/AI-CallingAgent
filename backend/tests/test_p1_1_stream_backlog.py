"""P1-1 -- the dial stream never drops an undelivered job.

Reproduction of the defect: `XADD ... MAXLEN ~ 200_000` trimmed the OLDEST entries whether or
not any consumer had read them (measured: 49,995 of 250,000 queued jobs gone, contacts left
PENDING behind a live one-hour enqueue guard). The fix is to never trim by length, release an
entry only when it is acknowledged, and bound the backlog by backpressure that leaves the
excess contacts un-enqueued (still PENDING) in PostgreSQL.

CI size is small; set P1_BACKLOG_JOBS=250000 for the full-size run.
"""

import os
import uuid

import pytest

from app.core.config import get_settings
from app.models.enums import ContactStatus
from app.services.queue import redis_queue
from app.services.queue.enqueue_service import enqueue_guard_key
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import QueueBacklogFull, RedisStreamQueue
from tests.test_cp11_concurrency import (
    _attempts_by_contact,
    _drain,
    _engine,
    _SpyProvider,
)
from tests.test_cp12c_enqueue_pagination import _campaign, _run

N_JOBS = int(os.environ.get("P1_BACKLOG_JOBS", "20000"))


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "mutation_rate_limit_per_minute", 10_000)
    yield
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


def _job(campaign_id: uuid.UUID) -> DialJob:
    return DialJob.new(campaign_id=campaign_id, contact_id=uuid.uuid4(), attempt_number=1)


def _queued_keys(redis_client, stream: str) -> list[str]:
    return [
        DialJob.from_json(fields["job"]).idempotency_key
        for _id, fields in redis_client.xrange(stream)
    ]


@pytest.mark.parametrize("path", ["enqueue_once", "enqueue"])
def test_undelivered_jobs_are_never_trimmed(redis_client, monkeypatch, path):
    # Shrinks the old length cap so the reproduction needs 20k adds, not 200k. On fixed code
    # the constant no longer exists (raising=False) and nothing trims at all.
    monkeypatch.setattr(redis_queue, "_STREAM_MAXLEN", 1_000, raising=False)
    queue = RedisStreamQueue(redis_client, "p1:trim", "g")
    campaign_id = uuid.uuid4()
    accepted = set()
    for _ in range(N_JOBS):
        job = _job(campaign_id)
        if path == "enqueue_once":
            try:
                assert queue.enqueue_once(job, enqueue_guard_key(job.idempotency_key), 3600)
            except QueueBacklogFull:
                break  # backpressure: refused, not dropped (only reachable above the cap)
        else:
            queue.enqueue(job)
        accepted.add(job.idempotency_key)

    keys = _queued_keys(redis_client, "p1:trim")
    assert len(keys) == len(accepted), f"{len(accepted) - len(keys)} accepted jobs were dropped"
    assert set(keys) == accepted
    if path == "enqueue":
        assert len(keys) == N_JOBS
    else:
        assert len(keys) == min(N_JOBS, get_settings().queue_max_backlog)


def test_acknowledged_entries_leave_the_stream(redis_client):
    queue = RedisStreamQueue(redis_client, "p1:ack", "g")
    campaign_id = uuid.uuid4()
    for _ in range(50):
        queue.enqueue(_job(campaign_id))

    for _ in range(50):
        read = queue.read_one("c1", block_ms=50)
        assert read is not None
        queue.ack(read[0])

    assert redis_client.xpending("p1:ack", "g")["pending"] == 0
    assert redis_client.xlen("p1:ack") == 0, "acked entries must not accumulate in the stream"


def test_unacknowledged_entries_stay_until_acked(redis_client):
    queue = RedisStreamQueue(redis_client, "p1:pending", "g")
    campaign_id = uuid.uuid4()
    for _ in range(10):
        queue.enqueue(_job(campaign_id))
    read = queue.read_one("c1", block_ms=50)
    assert read is not None  # delivered but NOT acked

    assert redis_client.xlen("p1:pending") == 10  # the in-flight job is still recoverable
    assert redis_client.xpending("p1:pending", "g")["pending"] == 1


def test_backlog_cap_pushes_back_instead_of_dropping(redis_client, monkeypatch, provider):
    monkeypatch.setattr(get_settings(), "queue_max_backlog", 250)
    campaign_id, contact_ids = _campaign(600)
    spy = _SpyProvider(provider)
    stream = get_queue().stream_key

    first = _run(campaign_id)
    assert first.enqueued == 250 and first.complete is False
    assert redis_client.xlen(stream) == 250  # every accepted job is still there
    # The 350 left behind are still enqueueable: no guard, still PENDING with no attempt.
    guarded = [
        c for c in contact_ids if redis_client.exists(enqueue_guard_key(f"{campaign_id}:{c}:1"))
    ]
    assert len(guarded) == 250

    for _ in range(3):  # 250 + 250 + 100: each pass is drained before the next
        _drain(spy, redis_client)
        if _run(campaign_id).enqueued == 0:
            break
    _drain(spy, redis_client)

    assert spy.calls == 600, "every contact must be dialed exactly once"
    assert set(_attempts_by_contact(contact_ids).values()) == {1}
    from tests.test_cp12b_paused_jobs import _contacts

    assert ContactStatus.PENDING not in {status for status, _ in _contacts(contact_ids).values()}
    assert redis_client.xlen(stream) == 0


def test_deferred_first_dial_is_rescheduled_not_dropped_when_backlog_is_full(
    redis_client, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.services.recovery import dispatch
    from app.services.recovery.scheduler import RecoveryScheduler

    campaign_id, (contact_id,) = _campaign(1)
    scheduler = RecoveryScheduler(redis_client)
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1)
    now = datetime.now(UTC)
    scheduler.schedule_dial(job, now - timedelta(seconds=1))

    queue = get_queue()
    monkeypatch.setattr(get_settings(), "queue_max_backlog", 1)
    queue.enqueue(_job(uuid.uuid4()))  # the stream is now at its cap

    assert dispatch.dispatch_due_recovery_jobs(scheduler, queue, now=now) == 0

    assert scheduler.pending_count() == 1, "the claimed schedule entry must be put back"
    assert redis_client.xlen(queue.stream_key) == 1
