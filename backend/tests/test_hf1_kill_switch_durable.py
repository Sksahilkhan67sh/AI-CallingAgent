"""HF1 1.3 -- the kill switch is durable: PostgreSQL is the source of truth.

Before: the switch lived only in Redis (no persistence in docker-compose), so a Redis restart
silently turned it OFF. Now the row `kill_switch` in `outbound_control` decides, together with
the OUTBOUND_KILL_SWITCH env backstop; an unreadable store fails CLOSED. Redis is no longer
consulted for this decision at all.
"""

import logging
import threading
from datetime import UTC, datetime

import pytest
import redis as redis_lib
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services import kill_switch, outbound_gate
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.phone_helpers import normalize_phone_number

_STREAM, _GROUP = "test:hf1:calls", "test:hf1:workers"


def _second_session():
    """A brand-new connection: what another process would see."""
    return create_engine(get_settings().primary_db_url).connect()


def _row():
    with _second_session() as conn:
        return conn.execute(
            text(
                "SELECT engaged, reason, engaged_by, released_by FROM outbound_control"
                " WHERE key = 'kill_switch'"
            )
        ).one_or_none()


@pytest.fixture
def unreadable_store(monkeypatch):
    """The switch's store (PostgreSQL) cannot be read."""

    def _boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("app.services.outbound_control._open_session", _boom)


# --- the reproduction: Redis loses its data -------------------------------------------


def test_repro_switch_survives_a_redis_flush(redis_client):
    assert kill_switch.enable("admin", "incident 1") is True
    redis_client.flushdb()  # a Redis restart without persistence
    redis_lib.Redis.from_url(get_settings().redis_url).flushall()
    assert kill_switch.block_reason() == kill_switch.ENABLED


def test_switch_survives_redis_being_unreachable_and_redis_is_not_consulted(monkeypatch):
    kill_switch.enable("admin", "x")

    def _no_redis(*args, **kwargs):
        raise redis_lib.RedisError("redis must not be consulted for the kill switch")

    monkeypatch.setattr("app.core.redis_client.get_redis", _no_redis)
    monkeypatch.setattr(redis_lib.Redis, "execute_command", _no_redis)
    assert kill_switch.block_reason() == kill_switch.ENABLED
    kill_switch.disable()
    assert kill_switch.block_reason() is None  # a dead Redis does not block dialing any more


# --- durable state, visible to another session -------------------------------------------


def test_engaged_state_is_stored_in_postgres_and_visible_to_a_second_session():
    assert _row() is None
    assert kill_switch.enable("alice", "provider incident") is True
    row = _row()
    assert (row.engaged, row.reason, row.engaged_by, row.released_by) == (
        True,
        "provider incident",
        "alice",
        None,
    )
    assert kill_switch.disable(actor="bob") is True
    row = _row()
    assert (row.engaged, row.released_by) == (False, "bob")


def test_re_engaging_after_release_clears_the_release_fields():
    kill_switch.enable("alice", "one")
    kill_switch.disable(actor="bob")
    assert kill_switch.enable("carol", "two") is True
    with _second_session() as conn:
        released_by, released_at, engaged_by = conn.execute(
            text(
                "SELECT released_by, released_at, engaged_by FROM outbound_control"
                " WHERE key='kill_switch'"
            )
        ).one()
    assert (released_by, released_at, engaged_by) == (None, None, "carol")


def test_reason_is_bounded():
    kill_switch.enable("admin", "x" * 500)
    assert len(_row().reason) == 200


# --- fail closed ---------------------------------------------------------------------------


def test_unreadable_store_blocks_and_logs_an_error(unreadable_store, caplog):
    with caplog.at_level(logging.ERROR):
        assert kill_switch.block_reason() == kill_switch.UNAVAILABLE
    assert any(r.message == "kill_switch_unreadable_failing_closed" for r in caplog.records)
    assert not any("connection refused" in r.getMessage() for r in caplog.records)


def test_the_shared_gate_blocks_when_the_store_is_unreadable(db_session, unreadable_store):
    assert outbound_gate.block_reason(db_session, datetime.now(UTC)) == kill_switch.UNAVAILABLE


def test_env_backstop_still_blocks_with_an_empty_or_unreadable_store(monkeypatch):
    monkeypatch.setenv("OUTBOUND_KILL_SWITCH", "true")
    get_settings.cache_clear()
    try:
        assert kill_switch.block_reason() == kill_switch.ENABLED  # nothing in the DB
        monkeypatch.setattr(
            "app.services.outbound_control._open_session",
            lambda: (_ for _ in ()).throw(OperationalError("x", {}, Exception("down"))),
        )
        assert kill_switch.block_reason() == kill_switch.ENABLED  # backstop wins, no DB needed
    finally:
        monkeypatch.delenv("OUTBOUND_KILL_SWITCH")
        get_settings.cache_clear()


# --- cache (injectable clock) ----------------------------------------------------------------


@pytest.fixture
def cached(monkeypatch):
    from app.services import outbound_control

    clock = {"now": 1000.0}
    monkeypatch.setattr(outbound_control, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(get_settings(), "kill_switch_cache_seconds", 2.0)
    outbound_control.reset_state()
    return clock


def test_a_change_made_elsewhere_is_seen_after_the_cache_window(cached):
    assert kill_switch.block_reason() is None  # populates the cache
    with create_engine(get_settings().primary_db_url).begin() as conn:  # "another process"
        conn.execute(
            text(
                "INSERT INTO outbound_control (key, engaged, reason, engaged_by, engaged_at)"
                " VALUES ('kill_switch', true, 'other process', 'ops', now())"
            )
        )
    cached["now"] += 1.0
    assert kill_switch.block_reason() is None  # still inside the 2 s window
    cached["now"] += 1.1
    assert kill_switch.block_reason() == kill_switch.ENABLED


def test_a_change_made_in_this_process_is_seen_immediately(cached):
    assert kill_switch.block_reason() is None
    kill_switch.enable("admin", "now")
    assert kill_switch.block_reason() == kill_switch.ENABLED
    kill_switch.disable(actor="admin")
    assert kill_switch.block_reason() is None


def test_a_failed_read_is_never_cached(cached, monkeypatch):
    from app.services import outbound_control

    real = outbound_control._open_session
    monkeypatch.setattr(
        outbound_control,
        "_open_session",
        lambda: (_ for _ in ()).throw(OperationalError("x", {}, Exception("blip"))),
    )
    assert kill_switch.block_reason() == kill_switch.UNAVAILABLE
    monkeypatch.setattr(outbound_control, "_open_session", real)
    assert kill_switch.block_reason() is None  # recovered at once, no stale "unavailable"


# --- exactly one transition under concurrency ---------------------------------------------------


def _hammer(fn, workers=16):
    results, barrier = [], threading.Barrier(workers)

    def run():
        barrier.wait()
        results.append(fn())

    threads = [threading.Thread(target=run) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_concurrent_engage_and_release_each_produce_exactly_one_transition():
    assert _hammer(lambda: kill_switch.enable("a", None)).count(True) == 1
    assert _hammer(lambda: kill_switch.disable(actor="a")).count(True) == 1
    assert kill_switch.block_reason() is None


# --- queued jobs and retries are untouched while engaged ------------------------------------------


def _setup(db_session, phone="989-800-0001"):
    campaign = Campaign(name="hf1 durable", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    db_session.flush()
    return campaign, contact


def _queue(redis_client):
    return RedisStreamQueue(redis_client, _STREAM, _GROUP)


def _admission(redis_client):
    return AdmissionController(
        redis_client,
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )


def _run_one(db_session, redis_client, provider):
    return process_one_job(
        db_session,
        _queue(redis_client),
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        consumer_name="w1",
        block_ms=20,
    )


def _attempts(db_session, contact):
    from sqlalchemy import select

    return db_session.execute(
        select(CallAttempt).where(CallAttempt.contact_id == contact.id)
    ).scalars().all()


@pytest.mark.parametrize("attempt_number", [1, 2])
def test_engaged_switch_keeps_queued_jobs_and_retries_intact_even_after_a_redis_flush(
    db_session, redis_client, provider, attempt_number
):
    campaign, contact = _setup(db_session)
    _queue(redis_client).enqueue(
        DialJob.new(
            campaign_id=campaign.id, contact_id=contact.id, attempt_number=attempt_number
        )
    )
    kill_switch.enable("admin", "freeze")
    stream_before = redis_client.xlen(_STREAM)

    assert _run_one(db_session, redis_client, provider) == JobOutcome.OUTBOUND_BLOCKED

    assert redis_client.xlen(_STREAM) == stream_before == 1  # nothing discarded
    assert redis_client.xpending(_STREAM, _GROUP)["pending"] == 0  # nothing acked or parked
    assert _attempts(db_session, contact) == []
    kill_switch.disable(actor="admin")
    assert _run_one(db_session, redis_client, provider) == JobOutcome.ADMITTED_AND_DIALED


def test_a_job_already_read_stays_unacked_while_engaged(db_session, redis_client, provider):
    campaign, contact = _setup(db_session, "989-800-0002")
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)
    kill_switch.enable("admin", "freeze")
    outcome = process_claimed_job(
        db_session,
        queue,
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        message_id,
        job,
    )
    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert redis_client.xpending(_STREAM, _GROUP)["pending"] == 1  # not acked, not lost


# --- API: authorization, audit, durable bookkeeping -----------------------------------------------


def test_only_an_admin_can_engage_or_release_and_both_are_audited(
    client, operator_client, db_session
):
    from sqlalchemy import select

    denied = operator_client.post("/api/v1/admin/kill-switch", json={"reason": "x"})
    assert denied.status_code == 403
    assert operator_client.delete("/api/v1/admin/kill-switch").status_code == 403
    assert _row() is None  # a rejected request changed nothing

    assert client.post("/api/v1/admin/kill-switch", json={"reason": "drill"}).status_code == 200
    assert _row().engaged_by == "test-admin"
    assert client.delete("/api/v1/admin/kill-switch").json() == {"enabled": False, "changed": True}
    assert _row().released_by == "test-admin"

    actions = [
        a for a in db_session.execute(select(AuditLog.action)).scalars()
        if a.startswith("kill_switch.")
    ]
    assert actions == ["kill_switch.enabled", "kill_switch.disabled"]


def test_status_endpoint_reports_the_durable_state(client):
    client.post("/api/v1/admin/kill-switch", json={"reason": "drill"})
    state = client.get("/api/v1/admin/kill-switch").json()
    assert state["enabled"] is True and state["runtime_flag"] is True
    assert state["reason"] == "drill" and state["enabled_by"] == "test-admin"
    redis_lib.Redis.from_url(get_settings().redis_url).flushall()
    assert client.get("/api/v1/admin/kill-switch").json()["enabled"] is True
