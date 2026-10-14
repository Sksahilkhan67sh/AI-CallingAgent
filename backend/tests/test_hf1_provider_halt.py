"""HF1 1.5 -- provider CONFIGURATION errors must not burn contacts.

Audited defect: with a rejected Dograh API key (HTTP 401) and 30 queued contacts the Dograh
client was called 5 times, 5 contacts became terminal (CompletedPartial, never retried) and 25
stayed Pending waiting for the circuit breaker. A rotated key or exhausted quota burned
customers permanently.

Now: a provider-CONFIGURATION error (401/403 bad credential, 404 unknown trigger, 402 exhausted
quota, or a client that cannot be built) records the attempt with a sanitized category, leaves
the contact dialable with its retry budget untouched, keeps the job unacked, and engages
`provider_halt` on the FIRST such error (one audit event). Only an admin releases it. Per-contact
errors keep today's behavior, with a systemic detector (N consecutive permanent failures with
the same category across DIFFERENT contacts) as the safety net.

Real PostgreSQL and Redis; the Dograh HTTP boundary is a deterministic fake.
"""

import logging
import threading

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.services import kill_switch, outbound_control, outbound_gate
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job, process_one_job
from app.services.queue.job import DialJob
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhConfigurationError,
    DograhErrorCategory,
)
from tests.test_cp13_dograh_critical import _api_error, _contact_status, _dial, _due_retry
from tests.test_failure_injection import (
    FakeDograh,
    _admission,
    _attempts,
    _drive,
    _queue,
    _Session,
    _world,
)

C = DograhErrorCategory
SECRET = "dg_live_secret_abc123"
RAW_BODY = f"HTTP 401 body={{'detail': 'Invalid key {SECRET}'}}"
HELD_EVENT = "DOGRAH_PROVIDER_HALT_HELD"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    f = FakeDograh()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: f)
    monkeypatch.setattr("app.services.queue.dialer_worker.time.sleep", lambda _s: None)
    yield f
    get_settings.cache_clear()


def _bad_key() -> DograhApiError:
    return DograhApiError(401, RAW_BODY, category=C.AUTHENTICATION_ERROR)


def _enqueue_all(queue, campaign_id, contact_ids) -> None:
    for contact_id in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))


def _pump(redis_client, provider, queue, admission, limit=200) -> list[str]:
    """Drive the real worker loop until the stream is drained or the gate blocks it."""
    outcomes: list[str] = []
    for _ in range(limit):
        with _Session() as db:
            outcome = process_one_job(
                db,
                queue,
                admission,
                provider,
                CircuitBreaker(redis_client, "dograh"),
                consumer_name="w1",
                block_ms=10,
            )
        outcomes.append(outcome)
        if outcome in (JobOutcome.NO_JOB, JobOutcome.OUTBOUND_BLOCKED):
            break
    return outcomes


def _redrive_pending(redis_client, provider, queue, admission) -> list[str]:
    outcomes = []
    for message_id, job in queue.reclaim_stale("w2", idle_ms=0, count=100):
        outcomes.append(_drive(queue, admission, provider, redis_client, message_id, job))
    return outcomes


def _contacts(ids):
    with _Session() as s:
        return [s.get(Contact, i) for i in ids]


def _audit_count(action: str) -> int:
    with _Session() as s:
        return s.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        ).scalar_one()


def _halt():
    from app.services import provider_halt

    return provider_halt.get_state()


# =============================================================================================
# the reproduction: 30 queued contacts + a rejected key
# =============================================================================================


def test_repro_rejected_key_with_30_contacts_burns_nobody(fake, redis_client, provider, caplog):
    campaign_id, ids = _world(30)
    queue, admission = _queue(redis_client, "halt1"), _admission(redis_client)
    fake.error = _bad_key()
    _enqueue_all(queue, campaign_id, ids)

    with caplog.at_level(logging.DEBUG):
        outcomes = _pump(redis_client, provider, queue, admission)

    assert fake.calls == 1  # exactly ONE provider call (it was 5)
    assert outcomes[0] == JobOutcome.PROVIDER_HALTED
    assert outcomes[-1] == JobOutcome.OUTBOUND_BLOCKED  # then the gate stopped the worker
    contacts = _contacts(ids)
    assert {c.status for c in contacts} == {ContactStatus.PENDING}  # zero terminal contacts
    assert {c.attempt_count for c in contacts} == {0}  # retry budget not consumed

    state = _halt()
    assert state["engaged"] is True
    assert state["category"] == "authentication_error"
    assert state["engaged_by"] == "system"
    assert state["reason"] and SECRET not in str(state)
    assert _audit_count("provider_halt.engaged") == 1  # one audit event

    # the held job is neither acked nor lost; the other 29 were never even read
    assert queue.redis.xpending(queue.stream_key, queue.group)["pending"] == 1
    assert queue.redis.xlen(queue.stream_key) == 30
    assert SECRET not in caplog.text


def test_the_held_attempt_is_recorded_with_a_sanitized_category(fake, redis_client, provider):
    campaign_id, ids = _world(2)
    queue, admission = _queue(redis_client, "halt2"), _admission(redis_client)
    fake.error = _bad_key()
    _enqueue_all(queue, campaign_id, ids)
    _pump(redis_client, provider, queue, admission)

    (attempt,) = (a for cid in ids for a in _attempts(cid))
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert attempt.provider_call_id is None
    with _Session() as s:
        events = s.execute(
            select(CallEvent).where(CallEvent.call_attempt_id == attempt.id)
        ).scalars().all()
    held = [e for e in events if e.event_type == HELD_EVENT]
    assert len(held) == 1
    assert held[0].payload["category"] == "authentication_error"
    assert held[0].payload["status_code"] == 401
    assert SECRET not in str([(e.event_type, e.payload) for e in events])


def test_no_credential_or_raw_body_reaches_audit_events_logs_or_api(
    fake, redis_client, provider, client, caplog
):
    campaign_id, ids = _world(3)
    queue, admission = _queue(redis_client, "halt3"), _admission(redis_client)
    fake.error = _bad_key()
    _enqueue_all(queue, campaign_id, ids)
    with caplog.at_level(logging.DEBUG):
        _pump(redis_client, provider, queue, admission)

    with _Session() as s:
        audit = str(
            [(a.action, a.event_metadata, a.actor) for a in s.execute(select(AuditLog)).scalars()]
        )
        calls = str([(e.event_type, e.payload) for e in s.execute(select(CallEvent)).scalars()])
    for haystack in (audit, calls, caplog.text, client.get("/api/v1/admin/kill-switch").text):
        assert SECRET not in haystack and "Invalid key" not in haystack


# =============================================================================================
# recovery after release
# =============================================================================================


def test_release_with_a_working_key_dials_everyone_exactly_once(fake, redis_client, provider):
    from app.services import provider_halt

    campaign_id, ids = _world(30)
    queue, admission = _queue(redis_client, "halt4"), _admission(redis_client)
    fake.error = _bad_key()
    _enqueue_all(queue, campaign_id, ids)
    _pump(redis_client, provider, queue, admission)
    assert fake.calls == 1

    fake.error = None  # the key was rotated
    assert provider_halt.release("admin", "key rotated") is True
    outcomes = _redrive_pending(redis_client, provider, queue, admission)
    outcomes += _pump(redis_client, provider, queue, admission)

    assert len(fake.run_ids) == 30  # everybody was dialed ...
    for cid in ids:  # ... exactly once, on exactly one attempt row
        (attempt,) = _attempts(cid)
        assert attempt.provider_call_id is not None
    assert {c.attempt_count for c in _contacts(ids)} == {1}
    assert JobOutcome.PROVIDER_HALTED not in outcomes


def test_contacts_are_rebuildable_from_postgres_after_a_redis_wipe(
    fake, redis_client, provider, client
):
    from app.services import provider_halt

    campaign_id, ids = _world(5)
    queue, admission = _queue(redis_client, "halt5"), _admission(redis_client)
    fake.error = _bad_key()
    _enqueue_all(queue, campaign_id, ids)
    _pump(redis_client, provider, queue, admission)

    redis_client.flushdb()  # every job reference is gone; PostgreSQL is all that is left
    fake.error = None
    provider_halt.release("admin", "rotated")

    response = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue")
    assert response.status_code == 200 and response.json()["enqueued"] == 5
    _pump(redis_client, provider, _queue(redis_client, "halt5b"), admission)

    assert len(fake.run_ids) == 5
    assert all(len(_attempts(cid)) == 1 for cid in ids)  # the held attempt row was reused


def test_a_held_retry_restores_its_exact_prior_state(fake, redis_client, provider):
    from app.services import provider_halt

    unavailable = _api_error(503, C.PROVIDER_UNAVAILABLE)
    _o, sc = _dial(fake, redis_client, provider, "halt6", error=unavailable)
    queue, admission, _cid, contact_id = sc
    before = (_contact_status(contact_id), _contacts([contact_id])[0].attempt_count)

    message_id, job = _due_retry(sc)
    fake.error = _bad_key()
    outcome = _drive(queue, admission, provider, redis_client, message_id, job)

    assert outcome == JobOutcome.PROVIDER_HALTED
    assert (_contact_status(contact_id), _contacts([contact_id])[0].attempt_count) == before
    assert _halt()["engaged"] is True

    fake.error = None
    provider_halt.release("admin", "rotated")
    (reclaimed,) = queue.reclaim_stale("w3", idle_ms=0)
    done = _drive(queue, admission, provider, redis_client, *reclaimed)
    assert done == JobOutcome.ADMITTED_AND_DIALED
    assert _contacts([contact_id])[0].attempt_count == before[1] + 1


# =============================================================================================
# per-contact errors are unchanged; the systemic detector is the safety net
# =============================================================================================


def test_a_per_contact_error_keeps_todays_behavior_and_does_not_halt(fake, redis_client, provider):
    error = _api_error(422, C.VALIDATION_ERROR)
    _o, sc = _dial(fake, redis_client, provider, "halt7", error=error)
    _q, _a, _cid, contact_id = sc
    assert _contact_status(contact_id) != ContactStatus.PENDING  # terminal, as before
    assert _halt()["engaged"] is False
    assert _audit_count("provider_halt.engaged") == 0


def test_five_consecutive_permanent_failures_across_contacts_engage_a_systemic_halt(
    fake, redis_client, provider
):
    campaign_id, ids = _world(7)
    queue, admission = _queue(redis_client, "halt8"), _admission(redis_client)
    fake.error = _api_error(422, C.VALIDATION_ERROR)
    _enqueue_all(queue, campaign_id, ids)
    outcomes = _pump(redis_client, provider, queue, admission)

    assert fake.calls == 5  # the 6th and 7th were never sent to the provider
    assert outcomes[4] == JobOutcome.PROVIDER_HALTED
    state = _halt()
    assert state["engaged"] is True and state["category"] == "systemic_failure"
    fifth = _contacts([ids[4]])[0]  # the contact that tripped the detector is NOT burned
    assert (fifth.status, fifth.attempt_count) == (ContactStatus.PENDING, 0)
    assert {c.status for c in _contacts(ids[5:])} == {ContactStatus.PENDING}
    assert _audit_count("provider_halt.engaged") == 1


def test_a_success_in_between_resets_the_streak(fake, redis_client, provider):
    bad = _api_error(422, C.VALIDATION_ERROR)
    for i in range(4):
        _dial(fake, redis_client, provider, f"halt9a{i}", error=bad)
    _dial(fake, redis_client, provider, "halt9ok", error=None)  # a trigger the provider accepted
    for i in range(4):
        _dial(fake, redis_client, provider, f"halt9b{i}", error=bad)
    assert _halt()["engaged"] is False


def test_different_categories_do_not_chain_into_a_systemic_halt(fake, redis_client, provider):
    for i, category in enumerate([C.VALIDATION_ERROR, C.PROVIDER_REJECTED] * 3):
        status = 422 if category == C.VALIDATION_ERROR else 410
        _dial(fake, redis_client, provider, f"halt10-{i}", error=_api_error(status, category))
    assert _halt()["engaged"] is False


def test_the_systemic_threshold_is_configurable(fake, redis_client, provider, monkeypatch):
    monkeypatch.setattr(get_settings(), "provider_halt_systemic_threshold", 2)
    bad = _api_error(422, C.VALIDATION_ERROR)
    _dial(fake, redis_client, provider, "halt11a", error=bad)
    assert _halt()["engaged"] is False
    _dial(fake, redis_client, provider, "halt11b", error=bad)
    assert _halt()["engaged"] is True


# =============================================================================================
# other configuration errors, classification, gate
# =============================================================================================


def test_an_unbuildable_client_is_a_configuration_error_too(fake, redis_client, provider):
    fake.error = DograhConfigurationError("DOGRAH_API_KEY is not set")
    _o, sc = _dial(fake, redis_client, provider, "halt12", error=fake.error)
    _q, _a, _cid, contact_id = sc
    assert _contact_status(contact_id) == ContactStatus.PENDING
    assert _halt()["engaged"] is True and _halt()["category"] == "configuration_error"


@pytest.mark.parametrize(
    "status,category,expected",
    [
        (401, C.AUTHENTICATION_ERROR, "authentication_error"),
        (403, C.AUTHENTICATION_ERROR, "authentication_error"),
        (404, C.PROVIDER_REJECTED, "provider_rejected"),
        (402, C.VALIDATION_ERROR, "quota_exhausted"),
    ],
)
def test_configuration_statuses_halt_on_the_first_error(
    fake, redis_client, provider, status, category, expected
):
    _dial(fake, redis_client, provider, f"halt13-{status}", error=_api_error(status, category))
    assert _halt()["engaged"] is True and _halt()["category"] == expected


@pytest.mark.parametrize(
    "status,category",
    [
        (400, C.VALIDATION_ERROR),  # telephony unconfigured OR initiation failed (free text)
        (409, C.VALIDATION_ERROR),
        (422, C.VALIDATION_ERROR),
        (408, C.AMBIGUOUS_REQUEST),
        (429, C.RATE_LIMITED),
        (500, C.AMBIGUOUS_REQUEST),
        (503, C.PROVIDER_UNAVAILABLE),
    ],
)
def test_statuses_that_cannot_be_clearly_classified_stay_per_contact(status, category):
    from app.services.telephony.dograh_outcome import provider_configuration_category

    assert provider_configuration_category(_api_error(status, category)) is None


def test_the_existing_status_policy_table_is_untouched():
    from app.services.telephony.dograh_outcome import TriggerPolicy, classify_trigger_error

    failure = classify_trigger_error(_api_error(401, C.AUTHENTICATION_ERROR))
    assert failure.policy == TriggerPolicy.PERMANENT
    assert failure.reason_key == "provider_configuration_error"


def test_provider_halt_blocks_through_the_same_gate_as_the_kill_switch(db_session):
    from datetime import UTC, datetime

    from app.services import provider_halt

    assert outbound_gate.block_reason(db_session, datetime.now(UTC)) is None
    provider_halt.engage("authentication_error", "test")
    reason = outbound_gate.block_reason(db_session, datetime.now(UTC))
    assert reason == provider_halt.HALTED and reason != kill_switch.ENABLED
    assert not outbound_gate.is_budget_reason(reason)


def test_provider_halt_survives_a_redis_flush_and_blocks_a_job_already_read(
    fake, redis_client, provider
):
    from app.services import provider_halt

    campaign_id, (cid,) = _world(1)
    queue, admission = _queue(redis_client, "halt14"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))
    message_id, job = queue.read_one("w1", 100)
    provider_halt.engage("systemic_failure", "test")
    redis_client.flushdb()
    with _Session() as db:
        outcome = process_claimed_job(
            db, queue, admission, provider, CircuitBreaker(redis_client, "dograh"), message_id, job
        )
    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert fake.calls == 0 and _attempts(cid) == []


def test_concurrent_engagements_produce_one_transition_and_one_audit_event():
    from app.services import provider_halt

    results, barrier = [], threading.Barrier(16)

    def run():
        barrier.wait()
        results.append(provider_halt.engage("authentication_error", "flood"))

    threads = [threading.Thread(target=run) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert _audit_count("provider_halt.engaged") == 1


def test_reasons_are_a_fixed_vocabulary_never_exception_text():
    from app.services import provider_halt

    provider_halt.engage("authentication_error", "x" * 500)
    assert len(_halt()["reason"]) <= outbound_control.MAX_REASON_LENGTH


# =============================================================================================
# the admin API
# =============================================================================================


def test_status_endpoint_shows_both_flags(client):
    from app.services import provider_halt

    state = client.get("/api/v1/admin/kill-switch").json()
    assert state["provider_halt"]["engaged"] is False
    provider_halt.engage("quota_exhausted", "Provider quota exhausted (HTTP 402)")
    state = client.get("/api/v1/admin/kill-switch").json()
    assert state["enabled"] is False  # the kill switch itself is unchanged
    halt = state["provider_halt"]
    assert halt["engaged"] is True and halt["category"] == "quota_exhausted"
    assert halt["engaged_by"] == "system" and halt["engaged_at"] and halt["reason"]


def test_only_an_admin_can_release_and_a_reason_is_mandatory(client, operator_client):
    from app.services import provider_halt

    provider_halt.engage("authentication_error", "test")
    url = "/api/v1/admin/provider-halt/release"

    assert operator_client.post(url, json={"reason": "rotated"}).status_code == 403
    assert _halt()["engaged"] is True
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"reason": "   "}).status_code == 422
    assert client.post(url).status_code == 422
    assert _halt()["engaged"] is True  # nothing changed without a valid request

    ok = client.post(url, json={"reason": "key rotated, verified"})
    assert ok.status_code == 200 and ok.json() == {"engaged": False, "changed": True}
    state = _halt()
    assert state["engaged"] is False and state["released_by"] == "test-admin"

    with _Session() as s:
        row = s.execute(
            select(AuditLog).where(AuditLog.action == "provider_halt.released")
        ).scalar_one()
    assert row.actor == "test-admin" and row.event_metadata == {"reason": "key rotated, verified"}

    again = client.post(url, json={"reason": "again"})  # idempotent, no second audit event
    assert again.json()["changed"] is False
    assert _audit_count("provider_halt.released") == 1


def test_releasing_the_halt_does_not_touch_the_kill_switch(client):
    from app.services import provider_halt

    kill_switch.enable("admin", "separate")
    provider_halt.engage("authentication_error", "test")
    client.post("/api/v1/admin/provider-halt/release", json={"reason": "ok"})
    assert kill_switch.block_reason() == kill_switch.ENABLED  # still frozen by the operator


def test_anonymous_cannot_release(anon_client):
    response = anon_client.post("/api/v1/admin/provider-halt/release", json={"reason": "x"})
    assert response.status_code == 401
