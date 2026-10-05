"""CP11 -- concurrency and security races, with real threads, independent DB sessions
and real Redis (the same approach as test_concurrency_dialer / test_webhook_concurrency).

What these prove, and what they do NOT:
* They prove the invariants that the architecture can actually guarantee: no duplicate
  outbound call, no authorization bypass, exactly-one kill-switch transition and audit row,
  queued work preserved.
* They do NOT claim the kill switch is atomic with the outbound HTTP trigger. The honest
  bound is tested instead: once enable() has returned, no call is placed after a short grace
  period (one dial's worth of time) -- see test_calls_stop_shortly_after_the_switch_flips.

These tests COMMIT real rows (needed to exercise the real races), so a module-scoped
fixture truncates the test database afterwards; later tests never see this module's data.
"""

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.main import app
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.services import kill_switch
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job, process_one_job
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.conftest import _bearer

_engine = create_engine(os.environ["PRIMARY_DB_URL"])
_Session = sessionmaker(bind=_engine)

N_WORKERS = 8


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
    # budgets are not what these tests are about
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "mutation_rate_limit_per_minute", 10_000)
    yield
    redis_client.flushdb()


# --- helpers ---------------------------------------------------------------------------------


def _campaign_with_contacts(n: int, *, status=CampaignStatus.ACTIVE):
    with _Session() as s:
        campaign = Campaign(name="cp11 concurrency", status=status)
        s.add(campaign)
        s.flush()
        contacts = []
        for i in range(n):
            phone = f"555-{800 + i // 100:03d}-{i % 100:04d}"
            contact = Contact(
                campaign_id=campaign.id,
                phone_number=phone,
                normalized_phone_number=normalize_phone_number(phone),
                status=ContactStatus.PENDING,
            )
            s.add(contact)
            contacts.append(contact)
        s.commit()
        return campaign.id, [c.id for c in contacts]


def _open_admission(redis_client):
    """Admission limits are not what these tests are about (and production defaults --
    5 calls/sec/campaign -- would throttle a 120-contact test): open them wide, exactly as
    test_concurrency_dialer does. The real controller still runs; only its limits differ."""
    return AdmissionController(
        redis_client,
        global_cps_limit=100_000,
        campaign_cps_limit=100_000,
        provider_cps_limit=100_000,
        global_concurrency_limit=100_000,
        campaign_concurrency_limit=100_000,
        provider_concurrency_limit=100_000,
    )


class _SpyProvider:
    """Wraps the mock provider; records the monotonic time of every outbound call."""

    def __init__(self, provider):
        self._provider = provider
        self.name = provider.name
        self.times: list[float] = []
        self._lock = threading.Lock()

    def create_outbound_call(self, *args, **kwargs):
        with self._lock:
            self.times.append(time.monotonic())
        return self._provider.create_outbound_call(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._provider, item)

    @property
    def calls(self) -> int:
        return len(self.times)


def _run_threads(target, n=N_WORKERS):
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def wrapped(i):
        try:
            barrier.wait(timeout=10)
            target(i)
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test thread below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=90) for t in threads]
    assert not errors, errors


def _drain(spy, redis_client, *, workers=N_WORKERS, stop=None):
    """N dial workers pull from the real queue until it is empty (or `stop` is set)."""
    queue = get_queue()
    admission = _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, spy.name)
    outcomes: list[str] = []

    def worker(i):
        with _Session() as session:
            while stop is None or not stop.is_set():
                outcome = process_one_job(
                    session,
                    queue,
                    admission,
                    spy,
                    breaker,
                    consumer_name=f"cp11-w{i}",
                    block_ms=50,
                )
                session.commit()
                if outcome == JobOutcome.NO_JOB:
                    return
                outcomes.append(outcome)

    _run_threads(worker, workers)
    return outcomes


def _reclaim_all(spy, redis_client):
    """What worker.main does every ~50 iterations, run to exhaustion."""
    queue = get_queue()
    admission = _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, spy.name)
    with _Session() as session:
        while batch := queue.reclaim_stale("reclaim", 0):
            for message_id, job in batch:
                process_claimed_job(session, queue, admission, spy, breaker, message_id, job)
                session.commit()


def _attempts_by_contact(contact_ids):
    with _Session() as s:
        rows = s.execute(
            select(CallAttempt.contact_id, func.count()).where(
                CallAttempt.contact_id.in_(contact_ids)
            ).group_by(CallAttempt.contact_id)
        ).all()
    return dict(rows)


def _audit_count(action, **meta_filters):
    with _Session() as s:
        return s.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        ).scalar_one()


def _admin_post(path, **kw):
    with TestClient(app, headers=_bearer("admin")) as c:
        return c.post(path, **kw)


# --- 8 concurrent enqueues of one campaign ---------------------------------------------------


def test_concurrent_enqueue_of_one_campaign_never_produces_a_duplicate_call(
    redis_client, provider
):
    campaign_id, contact_ids = _campaign_with_contacts(12)
    responses: list = [None] * N_WORKERS

    def enqueue(i):
        responses[i] = _admin_post(f"/api/v1/campaigns/{campaign_id}/enqueue")

    _run_threads(enqueue)
    assert [r.status_code for r in responses] == [200] * N_WORKERS  # no 5xx, no 429

    spy = _SpyProvider(provider)
    _drain(spy, redis_client)

    per_contact = _attempts_by_contact(contact_ids)
    assert all(count == 1 for count in per_contact.values()), per_contact
    assert len(per_contact) == len(contact_ids)  # every contact dialed...
    assert spy.calls == len(contact_ids)  # ...exactly once, however many duplicates queued


def test_concurrent_enqueue_admin_vs_operator_never_lets_the_operator_through(
    redis_client, provider
):
    campaign_id, _ = _campaign_with_contacts(3)
    statuses: dict[int, tuple[str, int]] = {}

    def enqueue(i):
        role = "operator" if i % 2 else "admin"
        with TestClient(app, headers=_bearer(role)) as c:
            statuses[i] = (role, c.post(f"/api/v1/campaigns/{campaign_id}/enqueue").status_code)

    _run_threads(enqueue)
    assert {code for role, code in statuses.values() if role == "operator"} == {403}
    assert {code for role, code in statuses.values() if role == "admin"} == {200}


# --- kill switch during processing -------------------------------------------------------------


def test_switch_on_before_workers_start_blocks_every_worker_and_leaves_every_job_queued(
    redis_client, provider
):
    campaign_id, contact_ids = _campaign_with_contacts(16)
    queue = get_queue()
    for cid in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))
    kill_switch.enable("admin", None)

    spy = _SpyProvider(provider)
    outcomes: list[str] = []
    admission = _open_admission(redis_client)
    breaker = CircuitBreaker(redis_client, spy.name)

    def worker(i):
        with _Session() as session:
            outcomes.append(
                process_one_job(
                    session, queue, admission, spy, breaker,
                    consumer_name=f"blocked-w{i}", block_ms=50,
                )
            )
            session.commit()

    _run_threads(worker)

    assert spy.calls == 0
    assert outcomes.count(JobOutcome.OUTBOUND_BLOCKED) == N_WORKERS
    assert _attempts_by_contact(contact_ids) == {}  # nothing half-claimed
    stream = get_settings().queue_stream_key
    assert redis_client.xlen(stream) == len(contact_ids)  # none dropped
    assert redis_client.xpending(stream, "dialer-workers")["pending"] == 0  # none even read


def test_calls_stop_shortly_after_the_switch_flips_and_queued_jobs_survive(
    redis_client, provider
):
    """Workers dial continuously; an admin flips the switch mid-run. The bound that CAN be
    guaranteed: after enable() returns, plus a grace period of one dial's duration, no
    further call is placed. (It is not atomic with the trigger -- in-flight dials that
    already passed the final check complete.) Then disabling lets the backlog finish at full
    speed with each contact dialed exactly once."""
    total = 120
    campaign_id, contact_ids = _campaign_with_contacts(total)
    queue = get_queue()
    for cid in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))

    spy = _SpyProvider(provider)
    original = spy.create_outbound_call

    def slowish(*args, **kwargs):
        time.sleep(0.01)  # a dial is not instantaneous: keeps the workers busy mid-run
        return original(*args, **kwargs)

    spy.create_outbound_call = slowish  # type: ignore[method-assign]

    enabled_at: list[float] = []
    stop = threading.Event()

    def flipper():
        while spy.calls < 10:  # let the workers get going
            time.sleep(0.002)
        kill_switch.enable("admin", "race test")
        enabled_at.append(time.monotonic())
        time.sleep(1.0)  # keep the workers polling against an ON switch for a while
        stop.set()

    flip_thread = threading.Thread(target=flipper)
    flip_thread.start()
    _drain(spy, redis_client, stop=stop)
    flip_thread.join(timeout=30)

    assert enabled_at, "switch was never flipped (workers finished before 10 calls?)"
    grace = 0.5
    late = [t for t in spy.times if t > enabled_at[0] + grace]
    assert late == [], f"{len(late)} calls placed after the switch + grace period"
    dialed_before_block = spy.calls
    assert 10 <= dialed_before_block < total  # it really did stop part-way
    stream = get_settings().queue_stream_key
    assert redis_client.xlen(stream) >= total - dialed_before_block  # backlog preserved
    # Each worker can be holding ONE job it had already read when the switch flipped (stage 1
    # refuses it and leaves it unacked). That is bounded by the worker count -- never the
    # backlog -- and reclaim_stale (the worker's normal crash-recovery path) re-drives it.
    in_hand = redis_client.xpending(stream, "dialer-workers")["pending"]
    assert in_hand <= N_WORKERS, f"{in_hand} jobs parked in the pending list (> {N_WORKERS})"

    # Re-enable outbound: the backlog drains with plain reads, nothing dialed twice...
    kill_switch.disable()
    _drain(spy, redis_client)
    # ...and the few in-hand jobs are picked up by the worker's reclaim path.
    _reclaim_all(spy, redis_client)
    assert redis_client.xpending(stream, "dialer-workers")["pending"] == 0

    per_contact = _attempts_by_contact(contact_ids)
    assert len(per_contact) == total and set(per_contact.values()) == {1}
    assert spy.calls == total


def test_retry_jobs_are_blocked_by_a_switch_enabled_during_processing(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(8)
    queue = get_queue()
    for cid in contact_ids:  # attempt_number 2 == what recovery enqueues for a retry
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=2))
    kill_switch.enable("admin", None)
    spy = _SpyProvider(provider)
    _drain(spy, redis_client, workers=4, stop=_stop_after(1.0))
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert redis_client.xlen(get_settings().queue_stream_key) == len(contact_ids)


def _stop_after(seconds):
    event = threading.Event()
    threading.Timer(seconds, event.set).start()
    return event


# --- pause / suppression racing the enqueue ------------------------------------------------------


def test_campaign_paused_after_enqueue_stops_every_dial(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(10)
    assert _admin_post(f"/api/v1/campaigns/{campaign_id}/enqueue").status_code == 200

    with _Session() as s:  # operator pauses between enqueue and dial
        s.get(Campaign, campaign_id).status = CampaignStatus.PAUSED
        s.commit()

    spy = _SpyProvider(provider)
    _drain(spy, redis_client, stop=_stop_after(1.5))
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}


def test_contact_suppressed_after_enqueue_is_never_dialed(redis_client, provider):
    campaign_id, contact_ids = _campaign_with_contacts(10)
    assert _admin_post(f"/api/v1/campaigns/{campaign_id}/enqueue").status_code == 200

    suppressed = contact_ids[:3]
    with _Session() as s:
        for cid in suppressed:
            contact = s.get(Contact, cid)
            s.add(
                Suppression(
                    contact_id=cid,
                    phone_number=contact.normalized_phone_number,
                    reason="cp11 race test",
                    source=SuppressionSource.MANUAL_API,
                )
            )
        s.commit()

    spy = _SpyProvider(provider)
    _drain(spy, redis_client)
    per_contact = _attempts_by_contact(contact_ids)
    assert not set(per_contact) & set(suppressed)
    assert spy.calls == len(contact_ids) - len(suppressed)


# --- kill switch API under concurrency -----------------------------------------------------


def test_concurrent_enable_yields_one_transition_and_one_audit_row():
    results: list = [None] * N_WORKERS

    def enable(i):
        results[i] = _admin_post("/api/v1/admin/kill-switch", json={"reason": "burst"}).json()

    before = _audit_count("kill_switch.enabled")
    _run_threads(enable)
    assert sum(r["changed"] for r in results) == 1
    assert _audit_count("kill_switch.enabled") - before == 1
    assert kill_switch.block_reason() == kill_switch.ENABLED


def test_concurrent_disable_yields_one_transition_and_one_audit_row():
    kill_switch.enable("admin", None)
    results: list = [None] * N_WORKERS

    def disable(i):
        with TestClient(app, headers=_bearer("admin")) as c:
            results[i] = c.delete("/api/v1/admin/kill-switch").json()

    before = _audit_count("kill_switch.disabled")
    _run_threads(disable)
    assert sum(r["changed"] for r in results) == 1
    assert _audit_count("kill_switch.disabled") - before == 1
    assert kill_switch.block_reason() is None


def test_interleaved_enable_disable_leaves_consistent_state_and_audit(redis_client):
    """Whatever the interleaving, audit enable/disable counts differ by at most one and
    agree with the final state."""

    def toggle(i):
        with TestClient(app, headers=_bearer("admin")) as c:
            for _ in range(5):
                (c.post if i % 2 == 0 else c.delete)("/api/v1/admin/kill-switch")

    enabled_before = _audit_count("kill_switch.enabled")
    disabled_before = _audit_count("kill_switch.disabled")
    _run_threads(toggle)
    enabled = _audit_count("kill_switch.enabled") - enabled_before
    disabled = _audit_count("kill_switch.disabled") - disabled_before

    final_on = kill_switch.block_reason() == kill_switch.ENABLED
    assert enabled - disabled == (1 if final_on else 0)


# --- webhooks while the switch is on, and with a forged credential in the mix -------------


def _connected_call():
    from app.models.enums import CallAttemptState

    campaign_id, (contact_id,) = _campaign_with_contacts(1)
    run_id = int(time.time() * 1000) % 90_000_000 + 10_000_000
    with _Session() as s:
        contact = s.get(Contact, contact_id)
        contact.status = ContactStatus.DIALING
        attempt = CallAttempt(
            contact_id=contact_id,
            attempt_number=1,
            provider="dograh",
            provider_call_id=str(run_id),
            state=CallAttemptState.INITIATED,
        )
        s.add(attempt)
        s.commit()
        return str(attempt.id), run_id


def _webhook(attempt_id, run_id, secret):
    with TestClient(app) as c:
        return c.post(
            "/api/v1/webhooks/dograh/call-completed",
            json={"call_attempt_id": attempt_id, "workflow_run_id": run_id,
                  "call_status": "completed"},
            headers={"Authorization": f"Bearer {secret}"},
        )


def test_kill_switch_never_blocks_completion_webhooks_of_active_calls():
    """The switch stops NEW calls only; the result of a call already in progress must
    still be recorded."""
    attempt_id, run_id = _connected_call()
    kill_switch.enable("admin", None)
    response = _webhook(attempt_id, run_id, get_settings().dograh_webhook_secret)
    assert response.status_code == 200 and response.json()["outcome"] == "ended_normally"


def test_valid_and_forged_webhooks_racing_are_processed_exactly_once_and_audited_consistently():
    attempt_id, run_id = _connected_call()
    good = get_settings().dograh_webhook_secret
    responses: list = [None] * N_WORKERS

    def deliver(i):
        secret = good if i % 2 == 0 else "forged-secret-value"
        responses[i] = _webhook(attempt_id, run_id, secret)

    _run_threads(deliver)

    valid = [r for i, r in enumerate(responses) if i % 2 == 0]
    forged = [r for i, r in enumerate(responses) if i % 2 == 1]
    assert {r.status_code for r in forged} == {401}
    assert all(r.status_code == 200 for r in valid)
    outcomes = sorted(r.json()["outcome"] for r in valid)
    assert outcomes.count("ended_normally") == 1
    assert outcomes.count("already_processed") == len(valid) - 1

    with _Session() as s:
        duplicates = s.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "webhook.duplicate", AuditLog.entity_id == attempt_id
            )
        ).scalar_one()
    assert duplicates == len(valid) - 1  # one audit row per duplicate, none lost or doubled
