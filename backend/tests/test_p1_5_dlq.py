"""P1-5 -- poison messages end in a dead-letter stream; nothing else is disturbed.

Defects reproduced on the unfixed code:
  * an unreadable payload raised inside `read_one` and `reclaim_stale`, stayed pending, and every
    later reclaim aborted the whole batch again -- forever, blocking the jobs in the same batch;
  * a job whose processing always raised was reclaimed again and again with no limit.

The loop below is one worker iteration: reclaim-and-redrive, then read one job; an exception is
rolled back and the loop carries on, exactly like app/worker.py.
"""

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services.queue import dialer_worker
from app.services.queue.dialer_worker import process_one_job, reclaim_and_redrive
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.test_cp11_concurrency import _engine, _open_admission, _SpyProvider
from tests.test_cp12c_enqueue_pagination import _campaign, _run, _Session

MAX_DELIVERIES = 3


def _truncate_all() -> None:
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    _truncate_all()
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "queue_reclaim_idle_ms", 0)
    monkeypatch.setattr(settings, "queue_max_deliveries", MAX_DELIVERIES)
    yield
    _truncate_all()
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    _truncate_all()


def _cycles(spy, redis_client, n: int, consumer: str = "w1") -> None:
    queue = get_queue()
    admission = _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, spy.name)
    for _ in range(n):
        with _Session() as db:
            try:
                reclaim_and_redrive(db, queue, admission, spy, breaker, consumer)
            except Exception:  # noqa: BLE001 - the worker loop's `except Exception`
                db.rollback()
            try:
                process_one_job(
                    db, queue, admission, spy, breaker, consumer_name=consumer, block_ms=20
                )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()


def _dlq(redis_client):
    return redis_client.xrange(get_queue().dlq_key)


def _status(contact_id):
    with _Session() as s:
        return s.get(Contact, contact_id).status


def _audits(action="queue.dead_lettered") -> int:
    with _Session() as s:
        return s.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        ).scalar_one()


def test_a_poison_job_goes_to_the_dlq_and_never_blocks_the_others(
    redis_client, provider, monkeypatch
):
    campaign_id, contact_ids = _campaign(6)
    poison = contact_ids[0]
    spy = _SpyProvider(provider)
    real_dial = dialer_worker._dial

    def dial(db, provider_, breaker, job, *a, **kw):
        if job.contact_id == str(poison):
            raise RuntimeError("poison: this job always blows up")
        return real_dial(db, provider_, breaker, job, *a, **kw)

    monkeypatch.setattr(dialer_worker, "_dial", dial)
    assert _run(campaign_id).enqueued == 6

    _cycles(spy, redis_client, 40)

    assert spy.calls == 5, "every healthy job is dialed, the poison one never is"
    queue = get_queue()
    assert redis_client.xlen(queue.stream_key) == 0
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 0
    entries = _dlq(redis_client)
    assert len(entries) == 1
    _id, fields = entries[0]
    assert fields["reason"] == "max_deliveries_exceeded"
    assert int(fields["times_delivered"]) > MAX_DELIVERIES
    assert json.loads(fields["job"])["contact_id"] == str(poison)  # original payload preserved
    assert fields["original_id"] and fields["stream"] == queue.stream_key and fields["dead_at"]
    assert _status(poison) == ContactStatus.NEEDS_REVIEW
    assert _audits() == 1

    snapshot = (spy.calls, len(_dlq(redis_client)), _audits())
    _cycles(spy, redis_client, 10)  # it never loops
    assert (spy.calls, len(_dlq(redis_client)), _audits()) == snapshot


@pytest.mark.parametrize(
    "fields",
    [{"job": "this is not json"}, {"job": '{"unexpected": 1}'}, {"other": "no job field"}],
    ids=["not_json", "wrong_shape", "missing_field"],
)
def test_a_malformed_payload_goes_straight_to_the_dlq(redis_client, provider, fields):
    campaign_id, contact_ids = _campaign(3)
    spy = _SpyProvider(provider)
    queue = get_queue()
    redis_client.xadd(queue.stream_key, fields)
    assert _run(campaign_id).enqueued == 3

    _cycles(spy, redis_client, 6)

    assert spy.calls == 3, "the good jobs behind it are not blocked"
    entries = _dlq(redis_client)
    assert len(entries) == 1
    assert entries[0][1]["reason"] == "malformed_payload"
    assert entries[0][1]["job"] == fields.get("job", "")
    assert redis_client.xlen(queue.stream_key) == 0
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 0
    assert _audits() == 1


def test_a_malformed_payload_found_by_reclaim_does_not_abort_the_batch(redis_client, provider):
    campaign_id, contact_ids = _campaign(4)
    spy = _SpyProvider(provider)
    queue = get_queue()
    redis_client.xadd(queue.stream_key, {"job": "garbage"})
    assert _run(campaign_id).enqueued == 4
    # A dead worker read everything and acked nothing: all five sit in the pending list.
    redis_client.xreadgroup(queue.group, "dead-worker", {queue.stream_key: ">"}, count=10)
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 5

    with _Session() as db:
        done = reclaim_and_redrive(
            db,
            queue,
            _open_admission(redis_client),
            spy,
            CircuitBreaker(redis_client, spy.name),
            "w2",
        )

    assert done == 4 and spy.calls == 4
    assert len(_dlq(redis_client)) == 1
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 0


def test_a_transient_failure_still_gets_its_retries(redis_client, provider, monkeypatch):
    campaign_id, (contact_id,) = _campaign(1)
    spy = _SpyProvider(provider)
    real_dial = dialer_worker._dial
    failures = {"left": MAX_DELIVERIES - 1}

    def flaky(db, provider_, breaker, job, *a, **kw):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("database hiccup")
        return real_dial(db, provider_, breaker, job, *a, **kw)

    monkeypatch.setattr(dialer_worker, "_dial", flaky)
    assert _run(campaign_id).enqueued == 1

    _cycles(spy, redis_client, 12)

    assert spy.calls == 1, "dialed exactly once after the transient failures"
    assert _dlq(redis_client) == []
    assert _audits() == 0
    with _Session() as s:
        attempts = s.execute(
            select(func.count())
            .select_from(CallAttempt)
            .where(CallAttempt.contact_id == contact_id)
        ).scalar_one()
    assert attempts == 1


def test_a_job_that_is_deliberately_held_is_never_mistaken_for_poison(redis_client, provider):
    """A paused campaign leaves a retry job unacked on purpose; reclaim redrives it every cycle.
    Raw XPENDING delivery counts would dead-letter it after a few cycles; it must wait instead."""
    campaign_id, (contact_id,) = _campaign(1)
    with _Session() as s:
        s.get(Campaign, campaign_id).status = CampaignStatus.PAUSED
        s.get(Contact, contact_id).status = ContactStatus.RETRY_SCHEDULED
        s.get(Contact, contact_id).attempt_count = 1
        s.commit()
    spy = _SpyProvider(provider)
    queue = get_queue()
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=2))

    _cycles(spy, redis_client, 4 * MAX_DELIVERIES)

    assert _dlq(redis_client) == [], "a legitimate hold must never reach the dead-letter stream"
    assert redis_client.xpending(queue.stream_key, queue.group)["pending"] == 1
    assert spy.calls == 0
    assert _status(contact_id) == ContactStatus.RETRY_SCHEDULED


def test_queue_max_deliveries_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, queue_max_deliveries=0)
    assert uuid.uuid4()


def test_a_failed_delivery_reset_never_turns_a_hold_into_an_error(redis_client):
    """After a Redis flush the stream and group are gone; resetting a counter must not raise."""
    queue = get_queue()
    redis_client.flushdb()
    queue.reset_deliveries("1-0")  # NOGROUP is swallowed
