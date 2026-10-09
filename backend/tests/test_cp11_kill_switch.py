"""CP11 -- global outbound kill switch (app/services/kill_switch.py).

Behaviour under test: ON blocks every NEW outbound call at the final dial path,
leaves queued jobs and active calls alone, and an unreadable switch is treated
as ON (fail closed).
"""

import re
import threading
from pathlib import Path

import pytest
import redis as redis_lib
from sqlalchemy import select

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services import kill_switch
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.phone_helpers import normalize_phone_number

_STREAM, _GROUP = "test:ks:calls", "test:ks:workers"


# --- helpers ---------------------------------------------------------------


def _setup(db_session, phone="989-700-0001"):
    campaign = Campaign(name="kill switch test", status=CampaignStatus.ACTIVE)
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


def _run_one(db_session, redis_client, provider, consumer="w1"):
    return process_one_job(
        db_session,
        _queue(redis_client),
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        consumer_name=consumer,
        block_ms=50,  # a blocked poll sleeps this long (production: 1000 ms); keep tests fast
    )


def _enqueue(db_session, redis_client, campaign, contact, attempt_number=1):
    _queue(redis_client).enqueue(
        DialJob.new(
            campaign_id=campaign.id, contact_id=contact.id, attempt_number=attempt_number
        )
    )


def _attempts(db_session, contact):
    # Scoped to this test's contact: other suites commit real rows that outlive them.
    return (
        db_session.execute(select(CallAttempt).where(CallAttempt.contact_id == contact.id))
        .scalars()
        .all()
    )


def _attempt_snapshot(db_session, contact):
    return [(a.state, a.provider_call_id) for a in _attempts(db_session, contact)]


def _pending(redis_client):
    return redis_client.xpending(_STREAM, _GROUP)["pending"]


def _audit_actions(db_session):
    return [r.action for r in db_session.execute(select(AuditLog)).scalars()]


@pytest.fixture
def dead_redis(monkeypatch):
    """Make the kill switch's own Redis client unreachable (closed port)."""
    bad = redis_lib.Redis.from_url(
        "redis://127.0.0.1:1/0",
        decode_responses=True,
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )
    monkeypatch.setattr(kill_switch, "_client", lambda: bad)


@pytest.fixture
def static_backstop(monkeypatch):
    monkeypatch.setenv("OUTBOUND_KILL_SWITCH", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("OUTBOUND_KILL_SWITCH")
    get_settings.cache_clear()


# --- admin API: who can change it, auditing, idempotence -----------------------


def test_admin_enables_and_status_reflects_it(client):
    assert client.get("/api/v1/admin/kill-switch").json()["enabled"] is False
    response = client.post("/api/v1/admin/kill-switch", json={"reason": "provider incident"})
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "changed": True}
    state = client.get("/api/v1/admin/kill-switch").json()
    assert state["enabled"] is True
    assert state["reason"] == "provider incident"
    assert state["enabled_by"] == "test-admin"


def test_enable_with_no_body_is_accepted(client):
    assert client.post("/api/v1/admin/kill-switch").status_code == 200


def test_operator_cannot_change_but_can_read(operator_client):
    assert operator_client.post("/api/v1/admin/kill-switch").status_code == 403
    assert operator_client.delete("/api/v1/admin/kill-switch").status_code == 403
    assert operator_client.get("/api/v1/admin/kill-switch").status_code == 200
    assert kill_switch.block_reason() is None  # and nothing changed


def test_anonymous_gets_401(anon_client):
    assert anon_client.get("/api/v1/admin/kill-switch").status_code == 401
    assert anon_client.post("/api/v1/admin/kill-switch").status_code == 401
    assert anon_client.delete("/api/v1/admin/kill-switch").status_code == 401


def test_forged_role_in_body_cannot_enable(operator_client):
    response = operator_client.post(
        "/api/v1/admin/kill-switch", json={"role": "admin", "reason": "x"}
    )
    assert response.status_code == 403
    assert kill_switch.block_reason() is None


def test_enable_and_disable_are_audited_once_per_transition(client, db_session):
    assert client.post("/api/v1/admin/kill-switch", json={"reason": "r"}).json()["changed"]
    again = client.post("/api/v1/admin/kill-switch", json={"reason": "r"}).json()
    assert again == {"enabled": True, "changed": False}  # no duplicate transition
    assert client.delete("/api/v1/admin/kill-switch").json() == {"enabled": False, "changed": True}
    assert client.delete("/api/v1/admin/kill-switch").json() == {
        "enabled": False,
        "changed": False,
    }
    actions = [a for a in _audit_actions(db_session) if a.startswith("kill_switch.")]
    assert actions == ["kill_switch.enabled", "kill_switch.disabled"]
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "kill_switch.enabled")
    ).scalar_one()
    assert row.actor == "test-admin" and row.entity_type == "system"


def test_oversized_reason_is_rejected(client):
    response = client.post("/api/v1/admin/kill-switch", json={"reason": "x" * 201})
    assert response.status_code == 422


# --- worker: switch OFF / ON ------------------------------------------------------


def test_switch_off_dials_normally(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    assert _run_one(db_session, redis_client, provider) == JobOutcome.ADMITTED_AND_DIALED


def _stream_len(redis_client):
    return redis_client.xlen(_STREAM)


def test_switch_on_blocks_new_admission_and_leaves_the_job_in_the_stream(
    db_session, redis_client, provider
):
    """Stage 0: the job is not even READ. It stays in the stream (not in the pending list)
    so it resumes at full speed -- see test_a_frozen_backlog_does_not_migrate_to_pending."""
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    kill_switch.enable("admin", None)

    outcome = _run_one(db_session, redis_client, provider)

    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert _stream_len(redis_client) == 1  # still queued, nothing dropped
    assert _pending(redis_client) == 0  # ...and never read, so nothing parked in the PEL
    assert _attempts(db_session, contact) == []  # no half-claimed attempt left behind
    db_session.refresh(contact)
    assert contact.status == ContactStatus.PENDING and contact.attempt_count == 0


def test_a_frozen_backlog_does_not_migrate_to_the_pending_list(
    db_session, redis_client, provider
):
    """The reason stage 0 exists: reclaim_stale re-drives only 10 pending jobs at a time,
    so a backlog read-and-parked during a freeze would crawl after re-enable."""
    campaign, _ = _setup(db_session)
    contacts = [_setup(db_session, phone=f"989-{710 + i}-0001")[1] for i in range(20)]
    for contact in contacts:
        _enqueue(db_session, redis_client, campaign, contact)
    kill_switch.enable("admin", None)

    for _ in range(20):
        assert _run_one(db_session, redis_client, provider) == JobOutcome.OUTBOUND_BLOCKED
    assert (_stream_len(redis_client), _pending(redis_client)) == (20, 0)

    kill_switch.disable()  # resume: plain reads, full speed, no reclaim machinery needed
    outcomes = [_run_one(db_session, redis_client, provider) for _ in range(20)]
    assert outcomes == [JobOutcome.ADMITTED_AND_DIALED] * 20
    assert _pending(redis_client) == 0


def test_a_blocked_poll_does_not_hot_loop(db_session, redis_client, provider):
    import time

    kill_switch.enable("admin", None)
    started = time.monotonic()
    process_one_job(
        db_session,
        _queue(redis_client),
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        consumer_name="w1",
        block_ms=200,
    )
    assert time.monotonic() - started >= 0.18  # paced like an empty blocking read


# --- stage 1: a job that was already READ when the switch flipped ----------------------


def _read_one(redis_client):
    read = _queue(redis_client).read_one("w1", 100)
    assert read is not None
    return read


def test_stage1_blocks_a_job_read_before_the_flip_and_keeps_it_pending(
    db_session, redis_client, provider
):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    message_id, job = _read_one(redis_client)  # worker holds the job...
    kill_switch.enable("admin", None)  # ...admin flips the switch...

    outcome = process_claimed_job(  # ...worker now processes it
        db_session,
        _queue(redis_client),
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        message_id,
        job,
    )

    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert _pending(redis_client) == 1  # unacked: re-drivable by reclaim, never lost
    assert _attempts(db_session, contact) == []


def test_stage1_never_reaches_admission(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    message_id, job = _read_one(redis_client)
    kill_switch.enable("admin", None)

    class _SpyAdmission:
        def try_admit(self, **kwargs):
            raise AssertionError("admission must not be consulted while blocked")

    outcome = process_claimed_job(
        db_session,
        _queue(redis_client),
        _SpyAdmission(),
        provider,
        CircuitBreaker(redis_client, provider.name),
        message_id,
        job,
    )
    assert outcome == JobOutcome.OUTBOUND_BLOCKED


def test_a_job_left_pending_by_stage1_resumes_after_disable(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    message_id, job = _read_one(redis_client)
    kill_switch.enable("admin", None)
    queue = _queue(redis_client)
    breaker = CircuitBreaker(redis_client, provider.name)
    admission = _admission(redis_client)
    assert (
        process_claimed_job(db_session, queue, admission, provider, breaker, message_id, job)
        == JobOutcome.OUTBOUND_BLOCKED
    )

    kill_switch.disable()
    ((message_id, job),) = queue.reclaim_stale("w2", 0)
    outcome = process_claimed_job(db_session, queue, admission, provider, breaker, message_id, job)
    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert _pending(redis_client) == 0


def test_active_call_is_unaffected_by_enabling_the_switch(db_session, redis_client, provider):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    assert _run_one(db_session, redis_client, provider) == JobOutcome.ADMITTED_AND_DIALED
    db_session.refresh(contact)
    before = (contact.status, _attempt_snapshot(db_session, contact))

    kill_switch.enable("admin", None)

    db_session.refresh(contact)
    after = (contact.status, _attempt_snapshot(db_session, contact))
    assert before == after


def test_retry_job_cannot_bypass_the_switch(db_session, redis_client, provider):
    """Recovery re-enqueues retries as DialJobs (attempt_number > 1) -- they take
    the same worker path and are blocked identically."""
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact, attempt_number=2)
    kill_switch.enable("admin", None)
    assert _run_one(db_session, redis_client, provider) == JobOutcome.OUTBOUND_BLOCKED
    assert _attempts(db_session, contact) == []


# --- stage 2: the final admission point, and the race it exists for ---------------------------


def test_stage2_catches_a_flip_after_admission_and_before_the_claim(
    db_session, redis_client, provider, monkeypatch
):
    """Worker read OFF at stage 0 and stage 1, admission passed, THEN admin enables: only the
    final check inside _dial can stop it -- and it does, before any durable attempt claim."""
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    message_id, job = _read_one(redis_client)
    answers = iter([None, kill_switch.ENABLED])  # stage 1: OFF, stage 2: ON
    monkeypatch.setattr(kill_switch, "block_reason", lambda: next(answers))

    outcome = process_claimed_job(
        db_session,
        _queue(redis_client),
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        message_id,
        job,
    )

    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert _attempts(db_session, contact) == []  # blocked BEFORE the durable attempt claim
    assert _pending(redis_client) == 1  # unacked, preserved


def test_a_flip_between_stage0_and_stage1_is_caught_by_stage1(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    answers = iter([None, kill_switch.ENABLED])  # stage 0: OFF (reads the job), stage 1: ON
    monkeypatch.setattr(kill_switch, "block_reason", lambda: next(answers))

    assert _run_one(db_session, redis_client, provider) == JobOutcome.OUTBOUND_BLOCKED
    assert _attempts(db_session, contact) == [] and _pending(redis_client) == 1


class _ExplodingDograhClient:
    def trigger_call(self, *args, **kwargs):
        raise AssertionError("Dograh trigger must not be reached while the switch is on")


def test_dograh_trigger_is_never_reached_while_switch_is_on(
    db_session, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.telephony.factory.get_dograh_client", lambda: _ExplodingDograhClient()
    )
    try:
        campaign, contact = _setup(db_session)
        _enqueue(db_session, redis_client, campaign, contact)
        kill_switch.enable("admin", None)
        outcome = _run_one(db_session, redis_client, provider)
        assert outcome == JobOutcome.OUTBOUND_BLOCKED
        assert _attempts(db_session, contact) == []
    finally:
        get_settings.cache_clear()


def test_only_the_gated_dial_path_can_place_a_call():
    """Source guard: the two provider entry points (native create_outbound_call,
    Dograh trigger_call) may only be *called* from dialer_worker.py, which is where
    both kill-switch checks sit. A new caller elsewhere would bypass the switch."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"\.(create_outbound_call|trigger_call)\(")
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path.name != "dialer_worker.py" and pattern.search(path.read_text())
    ]
    assert offenders == []


# --- fail closed ---------------------------------------------------------------------


def test_unreadable_switch_is_reported_as_blocked(dead_redis):
    assert kill_switch.block_reason() == kill_switch.UNAVAILABLE


def test_worker_fails_closed_when_switch_state_is_unreadable(
    db_session, redis_client, provider, dead_redis
):
    campaign, contact = _setup(db_session)
    _enqueue(db_session, redis_client, campaign, contact)
    assert _run_one(db_session, redis_client, provider) == JobOutcome.OUTBOUND_BLOCKED
    assert _attempts(db_session, contact) == []
    assert (_stream_len(redis_client), _pending(redis_client)) == (1, 0)


def test_enqueue_fails_closed_with_503(client, db_session, dead_redis):
    campaign, _ = _setup(db_session)
    response = client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    assert response.status_code == 503
    assert "redis" not in response.text.lower()


def test_admin_api_reports_503_not_500_when_redis_is_down(client, dead_redis):
    assert client.get("/api/v1/admin/kill-switch").status_code == 503
    assert client.post("/api/v1/admin/kill-switch").status_code == 503
    assert client.delete("/api/v1/admin/kill-switch").status_code == 503


# --- enqueue ---------------------------------------------------------------------------


def test_enqueue_is_rejected_while_switch_is_on_and_works_after(client, db_session):
    campaign, _ = _setup(db_session)
    client.post("/api/v1/admin/kill-switch", json={"reason": "drill"})
    blocked = client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    assert blocked.status_code == 409
    assert "kill switch" in blocked.json()["detail"].lower()

    client.delete("/api/v1/admin/kill-switch")
    resumed = client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    assert resumed.status_code == 200
    assert resumed.json()["enqueued"] == 1


# --- static env backstop ---------------------------------------------------------------


def test_env_backstop_blocks_without_any_redis_flag(static_backstop):
    assert kill_switch.block_reason() == kill_switch.ENABLED


def test_env_backstop_still_blocks_when_redis_is_down(static_backstop, dead_redis):
    # Reported as ENABLED (deliberately on), not merely "unreadable".
    assert kill_switch.block_reason() == kill_switch.ENABLED


def test_runtime_disable_cannot_clear_the_env_backstop(client, static_backstop):
    state = client.get("/api/v1/admin/kill-switch").json()
    assert state["enabled"] is True and state["static_env_flag"] is True
    result = client.delete("/api/v1/admin/kill-switch").json()
    assert result["enabled"] is True


# --- concurrency: exactly one state transition ----------------------------------------


def _hammer(fn, workers=16):
    results, barrier = [], threading.Barrier(workers)

    def run():
        barrier.wait()
        results.append(fn())

    threads = [threading.Thread(target=run) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_concurrent_enable_produces_exactly_one_transition():
    assert _hammer(lambda: kill_switch.enable("a", None)).count(True) == 1


def test_concurrent_disable_produces_exactly_one_transition():
    kill_switch.enable("a", None)
    assert _hammer(lambda: kill_switch.disable()).count(True) == 1
