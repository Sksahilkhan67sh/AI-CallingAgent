"""CP12-B -- paused-campaign job safety, against REAL PostgreSQL and Redis with real threads.

Same approach and helpers as test_cp11_concurrency: rows are committed (the races need
independent sessions), so a module-scoped fixture truncates the test database afterwards.

What these prove: a durably PAUSED campaign admits no outbound call whatever Redis holds;
paused first-attempt jobs are rebuilt from PostgreSQL exactly once on resume; retries are
preserved; transitions are serialized, idempotent, audited and authorized.
What they do NOT prove: that a pause is atomic with an HTTP trigger that is already in
flight (one DB commit + one request wide, exactly like the kill switch -- see the notes), nor
production-scale throughput. The 100K test measures statement counts, not wall-clock speed.
"""

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.main import app
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services import kill_switch
from app.services.queue import dialer_worker
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job, process_one_job
from app.services.queue.enqueue_service import enqueue_guard_key
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.conftest import _bearer
from tests.phone_helpers import normalize_phone_number
from tests.test_cp11_concurrency import (
    N_WORKERS,
    _attempts_by_contact,
    _campaign_with_contacts,
    _drain,
    _engine,
    _open_admission,
    _run_threads,
    _Session,
    _SpyProvider,
)

LEASE_GLOBAL = "concurrency:lease:global"


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "mutation_rate_limit_per_minute", 10_000)
    yield
    redis_client.flushdb()


# -- helpers --------------------------------------------------------------------------------------


def _api(method: str, path: str, role: str = "admin", **kw):
    with TestClient(app, headers=_bearer(role)) as c:
        return getattr(c, method)(path, **kw)


def _set_status(campaign_id, status: str):
    return _api("post", f"/api/v1/admin/campaigns/{campaign_id}/status?new_status={status}")


def _status(campaign_id) -> CampaignStatus:
    with _Session() as s:
        return s.get(Campaign, campaign_id).status


def _stop_after(seconds: float) -> threading.Event:
    """Bounds a drain while the kill switch is on: workers then never reach NO_JOB."""
    event = threading.Event()
    threading.Timer(seconds, event.set).start()
    return event


def _enqueue(campaign_id):
    response = _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue")
    assert response.status_code == 200, response.text
    return response.json()


def _stream_len(redis_client) -> int:
    return redis_client.xlen(get_queue().stream_key)


def _pending(redis_client) -> int:
    return redis_client.xpending(get_queue().stream_key, get_queue().group)["pending"]


def _contacts(contact_ids):
    with _Session() as s:
        rows = s.execute(
            select(Contact.id, Contact.status, Contact.attempt_count).where(
                Contact.id.in_(contact_ids)
            )
        ).all()
    return {r.id: (r.status, r.attempt_count) for r in rows}


def _audit_rows(campaign_id):
    with _Session() as s:
        return list(
            s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == campaign_id,
                    AuditLog.action == "campaign.status_changed",
                )
            ).scalars()
        )


def _reclaim_pass(spy, redis_client):
    """One reclaim round, as worker.main runs it every ~50 iterations. A real idle threshold
    (not 0): with idle_ms=0 a never-acked HELD message is instantly re-claimable and XAUTOCLAIM
    would hand it back forever; production uses queue_reclaim_idle_ms (30s)."""
    time.sleep(0.15)  # let held messages cross the 100 ms idle threshold below
    queue, admission = get_queue(), _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, spy.name)
    outcomes = []
    with _Session() as session:
        for message_id, job in queue.reclaim_stale("reclaim", 100):
            outcomes.append(
                process_claimed_job(session, queue, admission, spy, breaker, message_id, job)
            )
            session.commit()
    return outcomes


# -- A / B / G / T: durable state, idempotency, audit ---------------------------------------------


def test_pause_and_resume_are_durable_idempotent_and_audited(redis_client):
    campaign_id, _ = _campaign_with_contacts(3)
    assert _set_status(campaign_id, "paused").status_code == 200
    assert _status(campaign_id) == CampaignStatus.PAUSED  # committed before the reply
    assert _set_status(campaign_id, "paused").status_code == 200  # repeat: safe, no-op
    assert _set_status(campaign_id, "paused").status_code == 200
    assert len(_audit_rows(campaign_id)) == 1  # one transition, one audit row

    assert _set_status(campaign_id, "active").status_code == 200
    assert _status(campaign_id) == CampaignStatus.ACTIVE
    assert _set_status(campaign_id, "active").status_code == 200
    rows = _audit_rows(campaign_id)
    assert len(rows) == 2

    pause_row = next(r for r in rows if r.event_metadata["to"] == "paused")
    assert pause_row.event_metadata["from"] == "active"
    assert pause_row.event_metadata["result"] == "applied"
    assert pause_row.event_metadata["request_id"]
    assert pause_row.actor == "test-admin" and pause_row.created_at is not None
    assert "jwt" not in str(pause_row.event_metadata).lower()


def test_invalid_transition_is_rejected_without_side_effects():
    campaign_id, _ = _campaign_with_contacts(1, status=CampaignStatus.DRAFT)
    assert _set_status(campaign_id, "paused").status_code == 422
    assert _status(campaign_id) == CampaignStatus.DRAFT
    assert _audit_rows(campaign_id) == []
    assert _set_status(uuid.uuid4(), "paused").status_code == 404


# -- C / D / E / F / N / U: queued jobs survive a pause and run exactly once on resume ------------


def test_paused_queue_is_preserved_and_resume_runs_every_job_exactly_once(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(10)
    assert _enqueue(campaign_id)["enqueued"] == 10
    assert _set_status(campaign_id, "paused").status_code == 200

    spy = _SpyProvider(provider)
    _drain(spy, redis_client)  # 8 workers consume while paused
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert _pending(redis_client) == 0  # nothing left in limbo ...
    assert set(_contacts(contact_ids).values()) == {(ContactStatus.PENDING, 0)}  # ... PG intact
    assert (
        redis_client.exists(*[enqueue_guard_key(f"{campaign_id}:{c}:1") for c in contact_ids]) == 0
    )

    assert _set_status(campaign_id, "active").status_code == 200  # re-queues the dropped jobs
    assert _stream_len(redis_client) >= 10
    _drain(spy, redis_client)
    assert spy.calls == 10
    assert set(_attempts_by_contact(contact_ids).values()) == {1}  # exactly one each


def test_duplicate_resume_creates_no_duplicate_queue_entries_or_calls(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(6)
    _enqueue(campaign_id)
    _set_status(campaign_id, "paused")
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)

    _set_status(campaign_id, "active")
    first = _stream_len(redis_client)
    _set_status(campaign_id, "active")
    _set_status(campaign_id, "active")
    assert _stream_len(redis_client) == first  # repeats re-queued nothing
    # and even a manual enqueue cannot duplicate: every live reference holds its guard
    assert _enqueue(campaign_id)["enqueued"] == 0
    _drain(spy, redis_client)
    assert spy.calls == 6
    assert set(_attempts_by_contact(contact_ids).values()) == {1}


def test_repeated_pause_does_not_touch_jobs_or_contacts(redis_client):
    campaign_id, contact_ids = _campaign_with_contacts(5)
    _enqueue(campaign_id)
    before = (_stream_len(redis_client), _contacts(contact_ids))
    for _ in range(3):
        assert _set_status(campaign_id, "paused").status_code == 200
    assert (_stream_len(redis_client), _contacts(contact_ids)) == before


def test_pause_never_deletes_unconsumed_queue_entries(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(8)
    _enqueue(campaign_id)
    _set_status(campaign_id, "paused")
    assert _stream_len(redis_client) == 8  # pause is a single row update, not a queue scan
    _set_status(campaign_id, "active")  # nothing was consumed -> nothing needs re-queuing
    assert _stream_len(redis_client) == 8
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)
    assert spy.calls == 8


# -- N / O: stale Redis state cannot out-vote PostgreSQL ------------------------------------------


def test_stale_redis_entry_for_a_paused_campaign_never_dials(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(1, status=CampaignStatus.PAUSED)
    get_queue().enqueue(
        DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    )
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert _contacts(contact_ids)[contact_ids[0]] == (ContactStatus.PENDING, 0)


def test_redis_loss_does_not_unpause_and_resume_rebuilds_from_postgres(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(4)
    _enqueue(campaign_id)
    _set_status(campaign_id, "paused")

    redis_client.flushall()  # Redis restarts empty: queue, guards, leases all gone
    assert _status(campaign_id) == CampaignStatus.PAUSED  # PostgreSQL still says paused
    spy = _SpyProvider(provider)
    get_queue().enqueue(  # a stray/replayed reference appears after the restart
        DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    )
    _drain(spy, redis_client)
    assert spy.calls == 0

    _set_status(campaign_id, "active")  # the logical jobs were never lost: PG rebuilds them
    _drain(spy, redis_client)
    assert spy.calls == 4
    assert set(_attempts_by_contact(contact_ids).values()) == {1}


# -- M: a paused job never takes a CP12-A lease ---------------------------------------------------


class _ForbiddenAdmission:
    def try_admit(self, **_kw):
        raise AssertionError("a paused campaign must never reach admission")

    def release(self, *_a, **_k):
        raise AssertionError("no lease was taken, so none can be released")


def test_paused_job_acquires_no_concurrency_lease(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(1, status=CampaignStatus.PAUSED)
    get_queue().enqueue(
        DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    )
    with _Session() as s:
        outcome = process_one_job(
            s,
            get_queue(),
            _ForbiddenAdmission(),
            provider,
            CircuitBreaker(redis_client, provider.name),
            consumer_name="m",
            block_ms=50,
        )
    assert outcome == JobOutcome.CAMPAIGN_PAUSED
    assert redis_client.zcard(LEASE_GLOBAL) == 0


# -- H / A-race: the pause lands between the worker's check and its admission ---------------------


def test_pause_landing_after_the_worker_check_still_blocks_the_call_and_frees_the_lease(
    redis_client, provider, monkeypatch
):
    campaign_id, contact_ids = _campaign_with_contacts(1)
    get_queue().enqueue(
        DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    )
    real_check = dialer_worker._campaign_is_paused

    def read_then_get_paused(db, cid):
        seen = real_check(db, cid)  # the worker reads ACTIVE ...
        assert seen is False
        assert _set_status(campaign_id, "paused").status_code == 200  # ... then admin pauses
        return seen  # ... and the worker proceeds on its stale answer

    monkeypatch.setattr(dialer_worker, "_campaign_is_paused", read_then_get_paused)
    spy = _SpyProvider(provider)
    with _Session() as s:
        outcome = process_one_job(
            s,
            get_queue(),
            _open_admission(redis_client),
            spy,
            CircuitBreaker(redis_client, spy.name),
            consumer_name="race",
            block_ms=50,
        )
    assert outcome == JobOutcome.CAMPAIGN_PAUSED  # _dial's own durable re-check caught it
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert redis_client.zcard(LEASE_GLOBAL) == 0  # the lease it had taken was released (CP12-A)
    assert _contacts(contact_ids)[contact_ids[0]] == (ContactStatus.PENDING, 0)


def test_two_workers_on_one_job_during_a_pause_never_dial_and_resume_dials_once(
    redis_client, provider
):
    campaign_id, contact_ids = _campaign_with_contacts(1)
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    queue = get_queue()
    queue.enqueue(job)
    queue.enqueue(job)  # the same logical job delivered twice
    _set_status(campaign_id, "paused")

    spy = _SpyProvider(provider)
    _drain(spy, redis_client, workers=2)
    assert spy.calls == 0

    _set_status(campaign_id, "active")
    _drain(spy, redis_client, workers=2)
    assert spy.calls == 1 and _attempts_by_contact(contact_ids) == {contact_ids[0]: 1}


# -- I: a call that is already running is not touched by a pause ----------------------------------


def test_pause_does_not_disturb_a_call_already_placed(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(1)
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=1)
    get_queue().enqueue(job)
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)
    assert spy.calls == 1
    with _Session() as s:
        before = s.execute(
            select(CallAttempt.id, CallAttempt.state, CallAttempt.provider_call_id)
        ).all()
    contact_before = _contacts(contact_ids)

    _set_status(campaign_id, "paused")  # mid-call
    with _Session() as s:
        after = s.execute(
            select(CallAttempt.id, CallAttempt.state, CallAttempt.provider_call_id)
        ).all()
    assert after == before and _contacts(contact_ids) == contact_before  # nothing was killed

    get_queue().enqueue(job)  # a duplicate delivery arriving while paused is simply acked
    _drain(spy, redis_client)
    assert spy.calls == 1 and _pending(redis_client) == 0


# -- J / K: retries survive a pause ---------------------------------------------------------------


def _retry_scenario():
    campaign_id, contact_ids = _campaign_with_contacts(1)
    with _Session() as s:
        contact = s.get(Contact, contact_ids[0])
        contact.status = ContactStatus.RETRY_SCHEDULED
        contact.attempt_count = 1
        s.commit()
    return campaign_id, contact_ids


def test_paused_retry_is_held_untouched_and_dials_once_after_resume(redis_client, provider):
    campaign_id, contact_ids = _retry_scenario()
    get_queue().enqueue(
        DialJob.new(campaign_id=campaign_id, contact_id=contact_ids[0], attempt_number=2)
    )
    _set_status(campaign_id, "paused")
    spy = _SpyProvider(provider)

    _drain(spy, redis_client)  # a worker reads the retry job while paused
    assert spy.calls == 0
    assert _pending(redis_client) == 1  # held in the pending list, not acked
    assert _contacts(contact_ids)[contact_ids[0]] == (ContactStatus.RETRY_SCHEDULED, 1)
    assert _attempts_by_contact(contact_ids) == {}  # no failure/attempt recorded for the pause

    outcomes = _reclaim_pass(spy, redis_client)  # reclaim while still paused: still held
    assert outcomes == [JobOutcome.CAMPAIGN_PAUSED_HELD] and spy.calls == 0

    _set_status(campaign_id, "active")  # resume must NOT duplicate the retry
    assert _stream_len(redis_client) == 1
    assert _reclaim_pass(spy, redis_client) == [JobOutcome.ADMITTED_AND_DIALED]
    assert spy.calls == 1 and _attempts_by_contact(contact_ids) == {contact_ids[0]: 1}
    assert _contacts(contact_ids)[contact_ids[0]][1] == 2  # attempt_count advanced exactly once
    assert _pending(redis_client) == 0


@pytest.mark.parametrize(
    ("attempt_number", "recovery_type"), [(2, None), (1, "RECONNECT")], ids=["retry", "recovery"]
)
def test_retry_and_recovery_jobs_are_never_dropped_even_on_a_fresh_looking_contact(
    redis_client, provider, attempt_number, recovery_type
):
    """Dropping a job is only safe when PostgreSQL can rebuild it (a first attempt). A retry or
    recovery job exists only in Redis / the recovery scheduler, so it must be HELD no matter
    what the contact row looks like."""
    campaign_id, contact_ids = _campaign_with_contacts(1, status=CampaignStatus.PAUSED)
    get_queue().enqueue(
        DialJob.new(
            campaign_id=campaign_id,
            contact_id=contact_ids[0],
            attempt_number=attempt_number,
            recovery_type=recovery_type,
        )
    )
    spy = _SpyProvider(provider)
    with _Session() as s:
        outcome = process_one_job(
            s,
            get_queue(),
            _open_admission(redis_client),
            spy,
            CircuitBreaker(redis_client, spy.name),
            consumer_name="held",
            block_ms=50,
        )
    assert outcome == JobOutcome.CAMPAIGN_PAUSED_HELD
    assert _pending(redis_client) == 1 and spy.calls == 0  # still in the pending list


# -- L: kill switch outranks resume ---------------------------------------------------------------


def test_resume_while_the_kill_switch_is_on_places_no_call(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(3)
    _enqueue(campaign_id)
    _set_status(campaign_id, "paused")
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)

    assert kill_switch.enable("test", "cp12b") is True
    assert _set_status(campaign_id, "active").status_code == 200  # the resume itself succeeds
    assert _status(campaign_id) == CampaignStatus.ACTIVE
    _drain(spy, redis_client, stop=_stop_after(1.0))
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert set(_contacts(contact_ids).values()) == {(ContactStatus.PENDING, 0)}  # still recoverable

    assert kill_switch.disable() is True  # outbound re-enabled: the normal enqueue rebuilds
    assert _enqueue(campaign_id)["enqueued"] == 3
    _drain(spy, redis_client)
    assert spy.calls == 3 and set(_attempts_by_contact(contact_ids).values()) == {1}


# -- P / Q / R: concurrent transitions ------------------------------------------------------------


def test_concurrent_pause_requests_yield_one_transition(redis_client):
    campaign_id, _ = _campaign_with_contacts(2)
    codes: list[int] = [0] * N_WORKERS

    def pause(i):
        codes[i] = _set_status(campaign_id, "paused").status_code

    _run_threads(pause)
    assert codes == [200] * N_WORKERS
    assert _status(campaign_id) == CampaignStatus.PAUSED
    assert len(_audit_rows(campaign_id)) == 1


def test_concurrent_resume_requests_yield_one_transition_and_no_duplicates(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(5)
    _enqueue(campaign_id)
    _set_status(campaign_id, "paused")
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)

    codes: list[int] = [0] * N_WORKERS

    def resume(i):
        codes[i] = _set_status(campaign_id, "active").status_code

    _run_threads(resume)
    assert codes == [200] * N_WORKERS
    assert len([r for r in _audit_rows(campaign_id) if r.event_metadata["to"] == "active"]) == 1
    _drain(spy, redis_client)
    assert spy.calls == 5 and set(_attempts_by_contact(contact_ids).values()) == {1}


def test_concurrent_pause_and_resume_serialize_to_a_consistent_state(redis_client):
    campaign_id, _ = _campaign_with_contacts(2)
    _set_status(campaign_id, "paused")  # start PAUSED so both directions are valid moves
    results: list = [None] * N_WORKERS

    def toggle(i):
        results[i] = _set_status(campaign_id, "active" if i % 2 else "paused").status_code

    _run_threads(toggle)
    assert set(results) <= {200}  # every request is a valid move or a harmless no-op
    rows = _audit_rows(campaign_id)
    pauses = sum(r.event_metadata["to"] == "paused" for r in rows)
    resumes = sum(r.event_metadata["to"] == "active" for r in rows)
    # The trail starts at ACTIVE (including the setup pause above), so valid moves strictly
    # alternate pause, resume, pause, ...: the final state is fully determined by the
    # committed audit rows -- there is no impossible sequence to observe.
    assert pauses - resumes in (0, 1)
    assert _status(campaign_id) == (
        CampaignStatus.PAUSED if pauses > resumes else CampaignStatus.ACTIVE
    )
    for r in rows:
        assert (r.event_metadata["from"], r.event_metadata["to"]) in {
            ("active", "paused"),
            ("paused", "active"),
        }


# -- S: authorization (CP11 preserved) ------------------------------------------------------------


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_non_admin_roles_cannot_pause_or_resume(role):
    campaign_id, _ = _campaign_with_contacts(1)
    for path in (f"/api/v1/admin/campaigns/{campaign_id}/status?new_status=paused",):
        assert _api("post", path, role=role).status_code in (401, 403)
    patch = _api("patch", f"/api/v1/campaigns/{campaign_id}", role=role, json={"status": "paused"})
    assert patch.status_code in (401, 403)
    assert _status(campaign_id) == CampaignStatus.ACTIVE


def test_unauthenticated_requests_cannot_pause():
    campaign_id, _ = _campaign_with_contacts(1)
    with TestClient(app) as c:
        assert (
            c.post(f"/api/v1/admin/campaigns/{campaign_id}/status?new_status=paused").status_code
            == 401
        )
        assert (
            c.patch(f"/api/v1/campaigns/{campaign_id}", json={"status": "paused"}).status_code
            == 401
        )
    assert _status(campaign_id) == CampaignStatus.ACTIVE


def test_public_patch_route_pauses_and_resumes_with_the_same_guarantees(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(3)
    _enqueue(campaign_id)
    assert (
        _api("patch", f"/api/v1/campaigns/{campaign_id}", json={"status": "paused"}).status_code
        == 200
    )
    spy = _SpyProvider(provider)
    _drain(spy, redis_client)
    assert spy.calls == 0
    assert (
        _api("patch", f"/api/v1/campaigns/{campaign_id}", json={"status": "active"}).status_code
        == 200
    )
    _drain(spy, redis_client)
    assert spy.calls == 3


# -- deterministic multi-worker pause/resume run --------------------------------------------------


def test_workers_pause_resume_phases_never_duplicate_or_lose_a_call(redis_client, provider):
    n = 40
    campaign_id, contact_ids = _campaign_with_contacts(n)
    _enqueue(campaign_id)
    spy = _SpyProvider(provider)

    # Phase 1: a pause that is already committed before any worker starts -- zero calls.
    _set_status(campaign_id, "paused")
    _drain(spy, redis_client)
    assert spy.calls == 0

    # Phase 2: eight concurrent resumes (barrier-released) re-queue; workers then drain.
    _run_threads(lambda _i: _set_status(campaign_id, "active"))
    _drain(spy, redis_client)
    assert spy.calls == n

    # Phase 3: pause again, replay every job once more (stale references): still nothing.
    _set_status(campaign_id, "paused")
    queue = get_queue()
    for cid in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))
    _drain(spy, redis_client)
    assert spy.calls == n
    _set_status(campaign_id, "active")
    _drain(spy, redis_client)

    assert spy.calls == n  # replays of already-dialed jobs are acked duplicates, never calls
    assert set(_attempts_by_contact(contact_ids).values()) == {1} and len(contact_ids) == n
    assert _pending(redis_client) == 0
    assert redis_client.zcard(LEASE_GLOBAL) == 0  # CP12-A: no lease leaked by any of it


# -- performance: pause is O(1) in campaign size --------------------------------------------------


def _bulk_contacts(campaign_id, count):
    rows = [
        {
            "id": uuid.uuid4(),
            "campaign_id": campaign_id,
            "phone_number": f"989-{i:07d}",
            "normalized_phone_number": normalize_phone_number(f"989-{i:07d}") + f"-{i}",
            "status": ContactStatus.PENDING,
        }
        for i in range(count)
    ]
    with _engine.begin() as conn:
        conn.execute(Contact.__table__.insert(), rows)


def _pause_statements(campaign_id) -> list[str]:
    statements: list[str] = []

    def record(_conn, _cur, statement, *_a):
        statements.append(statement)

    # Class-level listener: the API request runs on the application's own engine, not _engine.
    event.listen(Engine, "before_cursor_execute", record)
    try:
        assert _set_status(campaign_id, "paused").status_code == 200
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    return statements


@pytest.mark.parametrize("size", [10, 1_000, 100_000])
def test_pause_does_no_work_proportional_to_campaign_size(redis_client, size):
    with _Session() as s:
        campaign = Campaign(name=f"size {size}", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.commit()
        campaign_id = campaign.id
    _bulk_contacts(campaign_id, size)
    with _Session() as s:
        assert (
            s.execute(
                select(func.count()).select_from(Contact).where(Contact.campaign_id == campaign_id)
            ).scalar_one()
            == size
        )

    started = time.monotonic()
    statements = _pause_statements(campaign_id)
    elapsed = time.monotonic() - started

    # The pause never reads or writes contacts or touches Redis: one locked campaign read, one
    # campaign update, one audit insert -- the same few statements at 10 and at 100,000 contacts.
    sql = [s.lower() for s in statements]
    assert any("update campaign" in s for s in sql), sql  # non-vacuous: we did see the pause
    assert any("for update" in s for s in sql), sql  # ... under the row lock
    assert not [s for s in sql if "contact" in s], sql
    assert len(statements) <= 8, statements  # constant: identical at 10 and at 100,000
    assert _status(campaign_id) == CampaignStatus.PAUSED
    print(f"pause of a {size}-contact campaign: {len(statements)} statements, {elapsed:.3f}s")
