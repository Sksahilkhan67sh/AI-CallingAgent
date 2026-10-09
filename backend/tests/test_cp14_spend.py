# ruff: noqa: F811
"""CP14 -- the basic daily dial / estimated-spend cap.

Everything money-shaped here is an ESTIMATE. The cap is enforced from PostgreSQL, fails
closed, and is explicitly NOT exact: see test_cache_ttl_bounds_overshoot_it_does_not_eliminate_it
and test_concurrent_dials_overshoot_by_at_most_the_number_in_flight."""

import itertools
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, ContactStatus
from app.services import outbound_gate, spend_cap
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job
from app.services.queue.job import DialJob
from app.services.recovery.scheduler import _SCHEDULE_KEY
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.cp14_helpers import (
    IST,
    Session,
    attempts_of,
    commit_world,
    contact_state,
    enqueue_first,
    ist,
    make_admission,
    make_queue,
    next_phone,
    production_window,  # noqa: F401  (fixture; a dependency of rig)
    rig,  # noqa: F401  (fixture)
    truncate_after_module,  # noqa: F401  (fixture)
)

_days = itertools.count(100, 5)  # every test that seeds attempts owns its own budget day


@pytest.fixture
def day() -> int:
    return next(_days)


@pytest.fixture(autouse=True)
def _budget(monkeypatch):
    """No cache, no spend cap, a roomy dial cap -- each test narrows what it is about."""
    s = get_settings()
    monkeypatch.setattr(s, "budget_check_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(s, "daily_dial_cap", 1_000_000)
    monkeypatch.setattr(s, "daily_estimated_spend_cap", None)
    monkeypatch.setattr(s, "estimated_cost_per_minute", None)
    monkeypatch.setattr(s, "estimated_minutes_per_unknown_attempt", 1.0)
    return s


def seed_attempts(n: int, started_at: datetime, duration: float | None = None) -> None:
    """n committed call attempts that 'started' at `started_at` (their own campaign)."""
    with Session() as s:
        campaign = Campaign(name="seed")
        s.add(campaign)
        s.flush()
        for _ in range(n):
            phone = next_phone()
            contact = Contact(
                campaign_id=campaign.id,
                phone_number=phone,
                normalized_phone_number=phone,
                status=ContactStatus.COMPLETED,
            )
            s.add(contact)
            s.flush()
            s.add(
                CallAttempt(
                    contact_id=contact.id,
                    attempt_number=1,
                    state=CallAttemptState.CONNECTED,
                    started_at=started_at,
                    duration_seconds=duration,
                )
            )
        s.commit()


def status_at(now: datetime):
    with Session() as s:
        return spend_cap.compute_status(s, now)


# -- configuration --------------------------------------------------------------------------------


def test_defaults_are_conservative_and_valid(monkeypatch):
    monkeypatch.delenv("DAILY_DIAL_CAP")  # the suite widens it; this asserts the shipped default
    s = Settings()
    assert s.daily_dial_cap == 1000
    assert s.daily_estimated_spend_cap is None and s.estimated_cost_per_minute is None
    assert s.budget_timezone == "Asia/Kolkata" and s.budget_check_cache_ttl_seconds == 2.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_estimated_spend_cap": 100.0},  # one without the other
        {"estimated_cost_per_minute": 1.0},
        {"daily_dial_cap": -1},
        {"daily_estimated_spend_cap": -1.0, "estimated_cost_per_minute": 1.0},
        {"daily_estimated_spend_cap": 1.0, "estimated_cost_per_minute": -1.0},
        {"estimated_minutes_per_unknown_attempt": -1},
        {"budget_check_cache_ttl_seconds": -1},
        {"budget_timezone": "Mars/Base"},
        {"default_timezone": "Nowhere"},
    ],
)
def test_bad_budget_config_is_a_startup_error(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_zero_is_a_valid_cap_and_both_spend_settings_together_are_valid():
    assert Settings(daily_dial_cap=0).daily_dial_cap == 0
    ok = Settings(daily_estimated_spend_cap=0.0, estimated_cost_per_minute=1.5)
    assert ok.daily_estimated_spend_cap == 0.0


# -- counting -------------------------------------------------------------------------------------


def test_budget_window_follows_the_budget_timezone_day():
    start, end, day = spend_cap.budget_window(
        datetime(2026, 1, 5, 20, 0, tzinfo=UTC), "Asia/Kolkata"
    )
    assert (day.isoformat(), start, end) == (
        "2026-01-06",
        datetime(2026, 1, 5, 18, 30, tzinfo=UTC),
        datetime(2026, 1, 6, 18, 30, tzinfo=UTC),
    )
    # a day with a DST change is 23 hours long, not 24
    start, end, _ = spend_cap.budget_window(
        datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "America/New_York"
    )
    assert end - start == timedelta(hours=23)


def test_dials_and_estimated_spend_are_counted_from_the_database(_budget, day):
    _budget.estimated_cost_per_minute = 2.0
    _budget.daily_estimated_spend_cap = 100.0
    _budget.estimated_minutes_per_unknown_attempt = 1.5
    first = ist(10, 0, day=day)
    seed_attempts(1, first, duration=120.0)
    seed_attempts(1, first + timedelta(hours=1), duration=60.0)
    seed_attempts(1, first + timedelta(hours=2), duration=None)  # unknown -> 1.5 minutes
    seed_attempts(2, ist(23, 59, day=day - 1))  # the previous budget day: not counted
    seed_attempts(2, ist(0, 0, day=day + 1))  # the next budget day (its first instant): not counted
    status = status_at(ist(12, 0, day=day))
    assert status.dials == 3
    assert status.estimated_minutes == 4.5  # 2 + 1 + 1.5
    assert status.estimated_spend == 9.0  # 4.5 min x 2.0


@pytest.mark.parametrize(
    ("day", "dial_cap", "spend_cap_", "expected"),
    [
        (14, 3, None, spend_cap.DAILY_DIAL_CAP_REACHED),  # exactly at the cap is "reached"
        (15, 4, None, None),
        (16, 1_000_000, 9.0, spend_cap.DAILY_SPEND_CAP_REACHED),
        (17, 1_000_000, 9.01, None),
    ],
)
def test_caps_are_reached_at_equality(_budget, day, dial_cap, spend_cap_, expected):
    # 3 attempts x 60 s = 3 minutes; at 3.0 per minute the estimate is exactly 9.0.
    seed_attempts(3, ist(10, 0, day=day), duration=60.0)
    _budget.daily_dial_cap = dial_cap
    if spend_cap_ is not None:
        _budget.daily_estimated_spend_cap = spend_cap_
        _budget.estimated_cost_per_minute = 3.0
    assert status_at(ist(12, 0, day=day)).dials == 3
    with Session() as s:
        assert spend_cap.block_reason(s, ist(12, 0, day=day)) == expected


def test_a_cap_of_zero_blocks_everything(_budget):
    _budget.daily_dial_cap = 0
    with Session() as s:
        assert spend_cap.block_reason(s, ist(12, 0, day=9)) == spend_cap.DAILY_DIAL_CAP_REACHED
    assert status_at(ist(12, 0, day=9)).percent_used == 100.0


# -- enforcement: the dialer ----------------------------------------------------------------------


def _backlog(rig, n, day):
    campaign, ids = commit_world(
        contacts=n, policy={"max_retries": 2, "retry_spacing_seconds": [900, 900]}
    )
    rig.clock.now = ist(12, 0, day=day)
    for c in ids:
        enqueue_first(rig, campaign, c)
    return campaign, ids


def test_cap_reached_blocks_reading_and_leaves_every_job_intact(rig, _budget, day):
    campaign, ids = _backlog(rig, 6, day)
    seed_attempts(3, ist(9, 0, day=day))
    _budget.daily_dial_cap = 3  # reached

    outcomes = {rig.run_one() for _ in range(4)}

    assert outcomes == {JobOutcome.BUDGET_BLOCKED}
    assert rig.redis.xlen(rig.queue.stream_key) == 6  # the whole backlog is still queued...
    assert rig.pending() == 0  # ...and NONE of it was pulled into the pending list
    assert all(attempts_of(c) == [] for c in ids)  # nothing claimed, nothing dialed
    assert all(contact_state(c) == (ContactStatus.PENDING, 0) for c in ids)  # no retry budget
    assert rig.redis.zcard(_SCHEDULE_KEY) == 0  # nothing rescheduled or dropped


def test_zero_cap_blocks_all_dialing_even_the_first_call(rig, _budget, day):
    campaign, ids = _backlog(rig, 1, day)
    _budget.daily_dial_cap = 0
    assert rig.run_one() == JobOutcome.BUDGET_BLOCKED
    assert attempts_of(ids[0]) == []


def test_dialing_resumes_by_itself_on_the_next_budget_day(rig, _budget, day):
    campaign, ids = _backlog(rig, 2, day)
    seed_attempts(2, ist(10, 0, day=day))
    _budget.daily_dial_cap = 2

    assert rig.run_one() == JobOutcome.BUDGET_BLOCKED  # 12:00: spent
    rig.clock.now = ist(23, 59, day=day)
    assert rig.run_one() == JobOutcome.BUDGET_BLOCKED  # still the same budget day
    with Session() as s:  # the budget day rolls over at local midnight -- nobody touches anything
        assert spend_cap.block_reason(s, ist(0, 1, day=day + 1)) is None
    rig.clock.now = ist(10, 0, day=day + 1)  # (and the 09-21 calling window is open again)
    assert rig.run_one() == JobOutcome.ADMITTED_AND_DIALED
    assert rig.run_one() == JobOutcome.ADMITTED_AND_DIALED
    assert sum(len(attempts_of(c)) for c in ids) == 2  # exactly the queued jobs, once each


def test_the_final_check_inside_dial_stops_a_cap_hit_after_admission(rig, monkeypatch, day):
    campaign, (contact,) = commit_world(policy=None)
    rig.clock.now = ist(12, 0, day=day)
    enqueue_first(rig, campaign, contact)
    message_id, job = rig.queue.read_one("w1", 50)
    answers = iter([None, spend_cap.DAILY_DIAL_CAP_REACHED])  # stage 1 open, stage 2 closed
    monkeypatch.setattr(outbound_gate, "block_reason", lambda db, now=None: next(answers))

    with Session() as s:
        outcome = process_claimed_job(
            s, rig.queue, rig.admission, rig.provider,
            CircuitBreaker(rig.redis, rig.provider.name), message_id, job,
        )  # fmt: skip
        s.commit()

    assert outcome == JobOutcome.BUDGET_BLOCKED
    assert attempts_of(contact) == []  # blocked BEFORE the durable attempt claim
    assert rig.pending() == 1  # left unacked, intact
    monkeypatch.undo()
    ((message_id, job),) = rig.queue.reclaim_stale("w2", 0)
    with Session() as s:
        resumed = process_claimed_job(
            s, rig.queue, rig.admission, rig.provider,
            CircuitBreaker(rig.redis, rig.provider.name), message_id, job,
        )  # fmt: skip
        s.commit()
    assert resumed == JobOutcome.ADMITTED_AND_DIALED and len(attempts_of(contact)) == 1


def test_an_unreadable_budget_fails_closed(rig, monkeypatch, day):
    campaign, ids = _backlog(rig, 2, day)

    def broken(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(spend_cap, "compute_status", broken)
    with Session() as s:
        assert spend_cap.block_reason(s, ist(12, 0, day=day)) == spend_cap.BUDGET_UNAVAILABLE
    assert rig.run_one() == JobOutcome.BUDGET_BLOCKED
    assert all(attempts_of(c) == [] for c in ids)
    assert rig.redis.xlen(rig.queue.stream_key) == 2 and rig.pending() == 0
    monkeypatch.undo()
    assert rig.run_one() == JobOutcome.ADMITTED_AND_DIALED  # and it recovers by itself


def test_the_kill_switch_still_comes_first_and_keeps_its_own_outcome(rig, monkeypatch, day):
    from app.services import kill_switch

    _backlog(rig, 1, day)
    monkeypatch.setattr(kill_switch, "block_reason", lambda: kill_switch.ENABLED)
    called = []
    monkeypatch.setattr(spend_cap, "block_reason", lambda *a, **k: called.append(1))
    assert rig.run_one() == JobOutcome.OUTBOUND_BLOCKED
    assert called == []  # the budget is not even computed while the switch is on


# -- the cache and the (documented) overshoot -----------------------------------------------------


def test_the_check_is_cached_for_the_ttl_and_invalidated_by_day_and_config(_budget, day):
    _budget.budget_check_cache_ttl_seconds = 60.0
    _budget.daily_dial_cap = 2
    started = ist(10, 0, day=day)
    seed_attempts(1, started)
    noon, next_noon = ist(12, 0, day=day), ist(12, 0, day=day + 1)
    with Session() as s:
        assert spend_cap.block_reason(s, noon) is None
        seed_attempts(1, started + timedelta(minutes=5))  # now at the cap...
        assert spend_cap.block_reason(s, noon) is None  # ...but cached for the TTL
        assert spend_cap.block_reason(s, next_noon) is None  # new day: recomputed (0)
        _budget.daily_dial_cap = 1  # a config change invalidates immediately
        assert spend_cap.block_reason(s, noon) == spend_cap.DAILY_DIAL_CAP_REACHED
        spend_cap.reset_state()
        _budget.daily_dial_cap = 2
        assert spend_cap.block_reason(s, noon) == spend_cap.DAILY_DIAL_CAP_REACHED


def test_cache_ttl_bounds_overshoot_it_does_not_eliminate_it(rig, _budget, day):
    """DOCUMENTED LIMITATION, asserted so the docs cannot drift from the code: a verdict cached
    as 'open' keeps dialing until the TTL expires, so a cap is overshot by at most the dials
    STARTED inside one TTL window -- here all of them, because the TTL never expires."""
    _budget.budget_check_cache_ttl_seconds = 3600.0
    _budget.daily_dial_cap = 1
    campaign, ids = _backlog(rig, 4, day)
    assert rig.run_one() == JobOutcome.ADMITTED_AND_DIALED  # count 0 < 1: open, cached
    dialed = 1 + sum(rig.run_one() == JobOutcome.ADMITTED_AND_DIALED for _ in range(3))
    assert dialed == 4  # cap was 1: overshoot == dials within the TTL window
    spend_cap.reset_state()
    assert rig.run_one() == JobOutcome.NO_JOB  # (queue empty) -- and a fresh check would block


def test_concurrent_dials_overshoot_by_at_most_the_number_in_flight(
    redis_client, provider, monkeypatch
):
    """With no cache, the only slack is the gap between the check and the attempt claim: at
    most one extra dial per concurrently running worker."""
    workers, jobs, headroom = 8, 40, 5
    campaign, ids = commit_world(
        contacts=jobs, policy={"max_retries": 0, "retry_spacing_seconds": []}
    )
    queue, admission = make_queue(redis_client, "overshoot"), make_admission(redis_client)
    for c in ids:
        queue.enqueue(DialJob.new(campaign_id=campaign, contact_id=c, attempt_number=1))
    before = status_at(datetime.now(UTC)).dials
    monkeypatch.setattr(get_settings(), "daily_dial_cap", before + headroom)

    def work():
        breaker = CircuitBreaker(redis_client, provider.name)
        for _ in range(jobs):
            with Session() as s:
                from app.services.queue.dialer_worker import process_one_job

                outcome = process_one_job(
                    s,
                    queue,
                    admission,
                    provider,
                    breaker,
                    consumer_name=threading.current_thread().name,
                    block_ms=10,
                )  # fmt: skip
                s.commit()
            if outcome in (JobOutcome.BUDGET_BLOCKED, JobOutcome.NO_JOB):
                return

    threads = [threading.Thread(target=work, name=f"w{i}") for i in range(workers)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]

    dialed = status_at(datetime.now(UTC)).dials - before
    assert dialed >= headroom  # the cap let the budget be used...
    assert dialed <= headroom + workers  # ...and overshoot never exceeded the documented bound
    untouched = sum(1 for c in ids if not attempts_of(c))
    assert untouched == jobs - dialed  # every job that was not dialed is still queued, intact
    assert redis_client.xlen(queue.stream_key) == jobs


# -- audit and log throttling ---------------------------------------------------------------------


def _iso(day: int) -> str:
    return ist(12, 0, day=day).astimezone(IST).date().isoformat()


def _budget_audits(day: str):
    with Session() as s:
        return [
            a
            for a in s.execute(
                select(AuditLog).where(AuditLog.action == "spend_cap.reached")
            ).scalars()
            if a.event_metadata["budget_day"] == day
        ]


def test_one_audit_event_per_day_per_cap_and_one_log_line_per_minute(
    redis_client, _budget, caplog, day
):
    caplog.set_level(logging.WARNING, logger="spend_cap")
    _budget.daily_dial_cap = 1
    seed_attempts(1, ist(9, 0, day=day))
    with Session() as s:
        for minute in range(30):  # a worker polling every few seconds all morning
            assert (
                spend_cap.block_reason(s, ist(10, minute, day=day))
                == spend_cap.DAILY_DIAL_CAP_REACHED
            )
    assert len(_budget_audits(_iso(day))) == 1
    assert len([r for r in caplog.records if r.message == "budget_cap_reached_not_dialing"]) == 1

    audit = _budget_audits(_iso(day))[0]
    assert audit.actor == "dialer-worker" and audit.event_metadata["cap"] == (
        spend_cap.DAILY_DIAL_CAP_REACHED
    )
    assert audit.event_metadata["dial_cap"] == 1

    seed_attempts(1, ist(9, 0, day=day + 1))
    with Session() as s:  # the next budget day, reached again: a NEW event
        spend_cap.block_reason(s, ist(10, 0, day=day + 1))
        spend_cap.block_reason(s, ist(10, 5, day=day + 1))
    assert len(_budget_audits(_iso(day + 1))) == 1

    _budget.estimated_cost_per_minute, _budget.daily_estimated_spend_cap = 1.0, 0.5
    _budget.daily_dial_cap = 1_000_000
    with Session() as s:  # a DIFFERENT cap on the same day is its own event
        assert (
            spend_cap.block_reason(s, ist(11, 0, day=day + 1)) == spend_cap.DAILY_SPEND_CAP_REACHED
        )
    assert len(_budget_audits(_iso(day + 1))) == 2


# -- the admin endpoint ---------------------------------------------------------------------------


def test_spend_endpoint_reports_position_for_any_role(
    client, operator_client, anon_client, _budget
):
    _budget.daily_dial_cap = 10_000_000  # real "today" already holds other tests' attempts
    _budget.estimated_cost_per_minute, _budget.daily_estimated_spend_cap = 2.0, 1e12
    body = operator_client.get("/api/v1/admin/spend").json()
    assert set(body) >= {
        "budget_day", "budget_timezone", "dials_today", "daily_dial_cap", "estimated_minutes",
        "estimated_spend", "daily_estimated_spend_cap", "percent_used", "blocked", "note",
    }  # fmt: skip
    assert body["daily_dial_cap"] == 10_000_000 and body["daily_estimated_spend_cap"] == 1e12
    assert "estimates" in body["note"].lower() and body["blocked"] is False
    assert client.get("/api/v1/admin/spend").status_code == 200
    assert anon_client.get("/api/v1/admin/spend").status_code == 401
    assert client.post("/api/v1/admin/spend").status_code == 405  # read-only


def test_spend_endpoint_reflects_a_reached_cap_and_never_uses_the_cache(client, _budget):
    _budget.budget_check_cache_ttl_seconds = 3600.0
    _budget.daily_dial_cap = 0
    body = client.get("/api/v1/admin/spend").json()
    assert body["blocked"] is True and body["percent_used"] == 100.0
    assert body["blocked_reason"] == spend_cap.DAILY_DIAL_CAP_REACHED


# -- call duration is persisted (the input to the estimate) ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12.5", 12.5), (95, 95.0), (0, 0.0), ("abc", None), (None, None), (-1, None),
     (float("nan"), None), (float("inf"), None), ("1e999", None), (10**9, 86_400.0)],
)  # fmt: skip
def test_provider_duration_is_parsed_defensively(raw, expected):
    from app.services.telephony.dograh_webhook_service import _parse_duration

    assert _parse_duration(raw) == expected


def test_webhook_persists_the_call_duration(client, db_session):
    from tests.test_dograh_webhook import _connected_call, _headers

    campaign, contact, attempt = _connected_call(db_session, phone="989-960-0777")
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "duration_seconds": 95,
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    db_session.refresh(attempt)
    assert attempt.duration_seconds == 95.0


_ = IST
