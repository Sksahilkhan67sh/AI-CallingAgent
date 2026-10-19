"""P1-10 -- the Phase 1 failure-injection gate.

Every scenario injects a failure into the real pipeline (WorkerLoop, dialer, queue, recovery,
sweeper) and then runs ONE invariant checker over PostgreSQL and Redis. After each scenario:

  1. no duplicate dial: every (contact, attempt number) was triggered at most once;
  2. no job lost: a PENDING contact whose enqueue guard exists has its job on the stream (a guard
     without a job would block re-enqueueing for an hour), a RETRY_SCHEDULED contact has a durable
     due time;
  3. every contact is terminal, or waiting in a recoverable way (queued / enqueueable / scheduled
     with a due time / dialing for less than the threshold); a `final` check allows only terminal;
  4. no attempt has been in flight beyond the stuck threshold.

Sizes: P1_FI_CONTACTS (default 12) and P1_FI_BACKLOG (default 300; 250000 for the full-size run).
The Postgres-restart scenario needs P1_PG_RESTART_CMD (a command that restarts the database and
returns once it is back); without it that scenario is skipped. Time is injected (a fake clock for
the loop, an explicit `now` for the sweeper); attempt start times are anchored to the Python clock
so the suite also passes under faketime.
"""

import json
import os
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, ContactStatus
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.queue import dialer_worker
from app.services.queue.dialer_worker import JobOutcome
from app.services.queue.enqueue_service import enqueue_guard_key
from app.services.queue.factory import get_queue
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.queue.stuck_call_sweeper import sweep_stuck_attempts
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryDecision, RecoveryManager
from app.services.recovery.rebuild import rebuild_retry_schedule
from app.services.recovery.scheduler import RecoveryScheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhTriggerResult
from app.services.telephony.dograh_webhook_service import process_dograh_webhook
from app.worker import WorkerLoop
from tests.test_cp11_concurrency import _engine, _open_admission
from tests.test_cp12c_enqueue_pagination import _campaign, _run, _Session
from tests.test_p1_2_durable_retry import _schedule_retries
from tests.test_p1_4_sweeper import _seed_stuck

N = int(os.environ.get("P1_FI_CONTACTS", "12"))
BACKLOG = int(os.environ.get("P1_FI_BACKLOG", "300"))
STUCK_AFTER = 3600
TERMINAL = {
    ContactStatus.COMPLETED,
    ContactStatus.COMPLETED_PARTIAL,
    ContactStatus.CLOSED,
    ContactStatus.NEEDS_REVIEW,
}
_RUN = iter(range(9_100_000, 9_900_000))


class _Killed(BaseException):
    """The process died here (passes through every `except Exception`, like a killed process)."""


class _Dograh:
    """Records every trigger by attempt id; can be armed to kill the worker once."""

    def __init__(self):
        self.triggers: Counter[str] = Counter()
        self.kill: str | None = None  # "before_trigger" | "after_trigger", consumed once

    def find_runs_for_attempt(self, call_attempt_id, since):
        return []

    def trigger_call(self, *, phone_number, initial_context):
        if self.kill == "before_trigger":
            self.kill = None
            raise _Killed
        self.triggers[initial_context["call_attempt_id"]] += 1
        run_id = next(_RUN)
        if self.kill == "after_trigger":
            self.kill = None
            raise _Killed
        return DograhTriggerResult(run_id, f"WR-{run_id}")


class _Tick:
    """Fake monotonic clock: drives the loop's reclaim / rebuild cadence, not the wall clock."""

    def __init__(self):
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t


def _truncate_all() -> None:
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _world(redis_client, monkeypatch):
    _truncate_all()
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "calling_engine", "dograh")
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000_000)
    monkeypatch.setattr(settings, "queue_reclaim_idle_ms", 0)
    monkeypatch.setattr(settings, "queue_reclaim_interval_seconds", 15.0)
    monkeypatch.setattr(settings, "queue_reclaim_jitter_ratio", 0.0)
    monkeypatch.setattr(settings, "queue_max_deliveries", 3)
    monkeypatch.setattr(settings, "job_deadline_seconds", 0)
    yield
    _truncate_all()
    redis_client.flushdb()


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    _truncate_all()


@pytest.fixture
def fake(monkeypatch):
    dograh = _Dograh()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: dograh)
    return dograh


# -- the harness ------------------------------------------------------------------------------


def _loop(redis_client, provider, tick):
    return WorkerLoop(
        queue=get_queue(),
        admission=_open_admission(redis_client),
        provider=provider,
        circuit_breaker=CircuitBreaker(redis_client, provider.name),
        recovery_scheduler=get_recovery_scheduler(),
        consumer_name="worker",
        session_factory=_Session,
        clock=tick,
        rng=lambda: 0.5,
        block_ms=10,
        heartbeat=lambda: None,
    )


def _drain(loop, tick, *, idle_stop: int = 12, max_steps: int = 600) -> int:
    """Step until the loop has been idle `idle_stop` times in a row. Returns steps taken."""
    idle = steps = 0
    while idle < idle_stop and steps < max_steps:
        tick.t += 20.0  # every step is 20 fake seconds: reclaim (15 s) is due each time
        outcome = loop.step()
        idle = idle + 1 if outcome in (JobOutcome.NO_JOB, None) else 0
        steps += 1
    return steps


def _anchor() -> None:
    """Attempt start times come from PostgreSQL's clock; the sweeper takes the Python clock."""
    with _Session() as s:
        s.execute(
            update(CallAttempt)
            .where(CallAttempt.state == CallAttemptState.INITIATED)
            .values(started_at=datetime.now(UTC))
        )
        s.commit()


def _complete_all() -> int:
    """Deliver the completion webhook for every attempt Dograh accepted."""
    done = 0
    with _Session() as s:
        attempts = (
            s.execute(
                select(CallAttempt).where(
                    CallAttempt.state == CallAttemptState.INITIATED,
                    CallAttempt.provider_call_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        payloads = [(str(a.id), int(a.provider_call_id)) for a in attempts]
    for attempt_id, run_id in payloads:
        with _Session() as s:
            process_dograh_webhook(
                s,
                DograhWebhookPayload(
                    call_attempt_id=attempt_id, workflow_run_id=run_id, call_status="completed"
                ),
            )
            s.commit()
            done += 1
    return done


def _make_retries(campaign_id, contact_ids, *, due_in_seconds: float = -60.0) -> None:
    """The first attempt failed; the contact waits RETRY_SCHEDULED (durable due time + cache)."""
    scheduler = RecoveryScheduler(get_queue().redis)
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
    # Make them due: the durable column is the truth, the ZSET entry is its cache.
    due = datetime.now(UTC) + timedelta(seconds=due_in_seconds)
    with _Session() as s:
        s.execute(update(Contact).where(Contact.id.in_(contact_ids)).values(next_retry_at=due))
        s.commit()
    scheduler.redis.delete("recovery:scheduled")
    rebuild_retry_schedule(_Session, scheduler)


def violations(
    redis_client, campaign_id, fake, *, now: datetime | None = None, final: bool = False
) -> list[str]:
    now = now or datetime.now(UTC)
    problems: list[str] = []
    queued: set[str] = set()
    for _id, fields in redis_client.xrange(get_queue().stream_key):
        try:
            queued.add(json.loads(fields["job"])["contact_id"])
        except (KeyError, ValueError):
            pass
    with _Session() as s:
        contacts = (
            s.execute(select(Contact).where(Contact.campaign_id == campaign_id)).scalars().all()
        )
        attempts = (
            s.execute(
                select(CallAttempt)
                .join(Contact, Contact.id == CallAttempt.contact_id)
                .where(Contact.campaign_id == campaign_id)
            )
            .scalars()
            .all()
        )
    by_contact = defaultdict(list)
    for a in attempts:
        by_contact[a.contact_id].append(a)

    # 1. no duplicate dial for one idempotency key
    key_of = {str(a.id): (str(a.contact_id), a.attempt_number) for a in attempts}
    per_key: Counter = Counter()
    for attempt_id, count in fake.triggers.items():
        if attempt_id in key_of:
            per_key[key_of[attempt_id]] += count
    problems += [f"duplicate dial {k}: {v} triggers" for k, v in per_key.items() if v > 1]

    for c in contacts:
        mine = by_contact[c.id]
        if c.status in TERMINAL:
            continue
        if final:
            problems.append(f"contact {c.id} is {c.status.value}, not terminal")
        if c.status == ContactStatus.PENDING:
            guarded = redis_client.exists(enqueue_guard_key(f"{campaign_id}:{c.id}:1"))
            if guarded and str(c.id) not in queued:  # 2. a guard with no job: the job was lost
                problems.append(f"contact {c.id}: enqueue guard but no job (lost)")
        elif c.status == ContactStatus.RETRY_SCHEDULED:
            if c.next_retry_at is None:  # 3. scheduled with a due time
                problems.append(f"contact {c.id}: RETRY_SCHEDULED without a due time")
        elif c.status == ContactStatus.DIALING:
            stale = [
                a
                for a in mine
                if a.state == CallAttemptState.INITIATED
                and a.started_at < now - timedelta(seconds=STUCK_AFTER)
            ]
            if stale:  # 4. in flight beyond the threshold
                problems.append(f"contact {c.id}: attempt in flight beyond the threshold")
        else:
            problems.append(f"contact {c.id}: unexpected status {c.status.value}")
    return problems


def _assert_ok(redis_client, campaign_id, fake, **kw):
    problems = violations(redis_client, campaign_id, fake, **kw)
    assert problems == [], "\n".join(problems[:15])


def _statuses(campaign_id) -> Counter:
    with _Session() as s:
        rows = s.execute(select(Contact.status).where(Contact.campaign_id == campaign_id))
        return Counter(r[0] for r in rows)


def _sweep(hours: float = 2.0):
    now = datetime.now(UTC) + timedelta(hours=hours)
    total = 0
    while True:
        result = sweep_stuck_attempts(_Session, now=now, stuck_after_seconds=STUCK_AFTER)
        total += result.actioned
        if result.actioned == 0:
            return total, now


# -- the checker itself must be able to fail ------------------------------------------------------


def test_the_checker_flags_each_invariant(redis_client, fake):
    campaign_id, ids = _campaign(3)
    # a guard with no job (lost), a retry with no due time, an attempt in flight too long
    redis_client.set(enqueue_guard_key(f"{campaign_id}:{ids[0]}:1"), "1")
    with _Session() as s:
        s.get(Contact, ids[1]).status = ContactStatus.RETRY_SCHEDULED
        s.get(Contact, ids[2]).status = ContactStatus.DIALING
        s.add(
            CallAttempt(
                contact_id=ids[2],
                attempt_number=1,
                state=CallAttemptState.INITIATED,
                started_at=datetime.now(UTC) - timedelta(hours=3),
            )
        )
        s.commit()
        attempt_id = str(s.execute(select(CallAttempt.id)).scalar_one())
    fake.triggers[attempt_id] = 2  # and a duplicate dial

    found = "\n".join(violations(redis_client, campaign_id, fake))

    assert "duplicate dial" in found
    assert "enqueue guard but no job" in found
    assert "without a due time" in found
    assert "in flight beyond the threshold" in found
    assert "not terminal" in "\n".join(violations(redis_client, campaign_id, fake, final=True))


# -- 1. kill the worker at each dial step ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["before_trigger", "after_trigger", "after_commit"])
def test_kill_the_worker_at_each_dial_step(mode, redis_client, provider, fake, monkeypatch):
    campaign_id, ids = _campaign(N)
    assert _run(campaign_id).enqueued == N
    tick = _Tick()
    if mode == "after_commit":
        killed = {"armed": True}
        real_ack = RedisStreamQueue.ack

        def ack(self, message_id):
            if killed["armed"]:
                killed["armed"] = False
                raise _Killed
            return real_ack(self, message_id)

        monkeypatch.setattr(RedisStreamQueue, "ack", ack)
    else:
        fake.kill = mode
    with pytest.raises(_Killed):
        _loop(redis_client, provider, tick).step()

    healthy = _loop(redis_client, provider, tick)  # a replacement worker takes over
    _drain(healthy, tick)
    _anchor()
    _complete_all()
    swept, now = _sweep()  # whatever the dead worker left that no webhook will ever settle

    _assert_ok(redis_client, campaign_id, fake, now=now, final=True)
    assert swept <= 1
    assert sum(fake.triggers.values()) <= N, "one dial per contact at most"
    _drain(healthy, tick)  # the dead worker's message is reclaimed and acked, never redialed
    assert redis_client.xpending(get_queue().stream_key, get_queue().group)["pending"] == 0
    _assert_ok(redis_client, campaign_id, fake, now=now, final=True)


# -- 2. Redis FLUSHALL in the middle of a campaign ------------------------------------------------


def test_redis_flushall_mid_campaign(redis_client, provider, fake):
    campaign_id, ids = _campaign(N)
    retries, fresh = ids[: N // 3], ids[N // 3 :]
    _make_retries(campaign_id, retries)
    assert _run(campaign_id).enqueued == len(fresh)
    tick = _Tick()
    loop = _loop(redis_client, provider, tick)
    for _ in range(3):
        tick.t += 20
        loop.step()

    redis_client.flushall()  # stream, group, guards, retry cache: all gone

    _assert_ok(redis_client, campaign_id, fake)  # nothing is lost, only displaced
    assert _run(campaign_id).complete  # the still-PENDING contacts are enqueueable again
    recovered = _loop(redis_client, provider, tick)  # start-up rebuild restores the retry cache
    _drain(recovered, tick, idle_stop=25)
    _anchor()
    _complete_all()
    _drain(recovered, tick, idle_stop=25)
    _anchor()
    _complete_all()

    _assert_ok(redis_client, campaign_id, fake, final=True)
    assert _statuses(campaign_id) == Counter({ContactStatus.COMPLETED: N})


# -- 3. a backlog far larger than the stream may hold ---------------------------------------------


@pytest.mark.slow
def test_a_backlog_beyond_the_cap_is_never_dropped(redis_client, provider, fake, monkeypatch):
    cap = max(BACKLOG // 3, 1)
    monkeypatch.setattr(get_settings(), "queue_max_backlog", cap)
    campaign_id, ids = _campaign(BACKLOG)
    queue = get_queue()

    first = _run(campaign_id)  # no consumer yet
    assert first.enqueued == cap and first.backlog_full and not first.complete
    assert redis_client.xlen(queue.stream_key) == cap, "every accepted job is on the stream"
    _assert_ok(redis_client, campaign_id, fake)

    if BACKLOG > 5_000:  # the full-size run proves retention, not throughput
        guarded = sum(
            1 for c in ids if redis_client.exists(enqueue_guard_key(f"{campaign_id}:{c}:1"))
        )
        assert guarded == cap, "the rest are un-enqueued, so still enqueueable"
        return

    tick = _Tick()
    loop = _loop(redis_client, provider, tick)
    for _ in range(BACKLOG // cap + 3):
        _drain(loop, tick, idle_stop=3, max_steps=BACKLOG * 2)
        _run(campaign_id)
    _drain(loop, tick, idle_stop=3, max_steps=BACKLOG * 2)
    _anchor()
    _complete_all()

    _assert_ok(redis_client, campaign_id, fake, final=True)
    assert sum(fake.triggers.values()) == BACKLOG
    assert _statuses(campaign_id) == Counter({ContactStatus.COMPLETED: BACKLOG})


# -- 4. duplicate delivery -----------------------------------------------------------------------


def test_duplicate_delivery_never_dials_twice(redis_client, provider, fake):
    campaign_id, ids = _campaign(N)
    assert _run(campaign_id).enqueued == N
    stream = get_queue().stream_key
    for _id, fields in list(redis_client.xrange(stream)):
        redis_client.xadd(stream, {"job": fields["job"]})  # the same job delivered again
    assert redis_client.xlen(stream) == 2 * N
    tick = _Tick()
    loop = _loop(redis_client, provider, tick)

    _drain(loop, tick)
    _anchor()
    _complete_all()

    _assert_ok(redis_client, campaign_id, fake, final=True)
    assert sum(fake.triggers.values()) == N
    assert max(fake.triggers.values()) == 1
    assert redis_client.xlen(stream) == 0


# -- 5. a poison message ------------------------------------------------------------------------


def test_a_poison_job_and_a_malformed_message_never_block_the_rest(
    redis_client, provider, fake, monkeypatch
):
    campaign_id, ids = _campaign(N)
    poison = ids[0]
    real_dial = dialer_worker._dial

    def dial(db, provider_, breaker, job, *a, **kw):
        if job.contact_id == str(poison):
            raise RuntimeError("poison")
        return real_dial(db, provider_, breaker, job, *a, **kw)

    monkeypatch.setattr(dialer_worker, "_dial", dial)
    redis_client.xadd(get_queue().stream_key, {"job": "not json"})
    assert _run(campaign_id).enqueued == N
    tick = _Tick()
    loop = _loop(redis_client, provider, tick)

    _drain(loop, tick, idle_stop=15)
    _anchor()
    _complete_all()

    _assert_ok(redis_client, campaign_id, fake, final=True)
    reasons = sorted(f["reason"] for _i, f in redis_client.xrange(get_queue().dlq_key))
    assert reasons == ["malformed_payload", "max_deliveries_exceeded"]
    assert sum(fake.triggers.values()) == N - 1
    assert _statuses(campaign_id)[ContactStatus.NEEDS_REVIEW] == 1
    assert redis_client.xpending(get_queue().stream_key, get_queue().group)["pending"] == 0


# -- 6. concurrent sweepers and rebuilders -----------------------------------------------------


def test_concurrent_sweepers_and_rebuilders_act_exactly_once(redis_client, fake):
    stuck_ids = _seed_stuck(40)
    retry_campaign, retry_ids, scheduler = _schedule_retries(redis_client, 30)
    redis_client.flushall()
    errors: list[BaseException] = []
    actioned: list[int] = []
    now = datetime.now(UTC) + timedelta(hours=2)

    def sweeper():
        try:
            while True:
                result = sweep_stuck_attempts(
                    _Session, now=now, stuck_after_seconds=STUCK_AFTER, batch_limit=7
                )
                actioned.append(result.actioned)
                if result.actioned == 0:
                    return
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    def rebuilder():
        try:
            rebuild_retry_schedule(_Session, RecoveryScheduler(redis_client), batch_size=7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=f) for f in (sweeper, sweeper, rebuilder, rebuilder)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]

    assert not errors
    assert sum(actioned) == 40, "every stuck attempt is actioned once across both sweepers"
    with _Session() as s:
        statuses = Counter(
            s.execute(select(Contact.status).where(Contact.id.in_(stuck_ids))).scalars()
        )
        audits = (
            s.execute(select(AuditLog).where(AuditLog.action == "sweeper.needs_review"))
            .scalars()
            .all()
        )
    assert statuses == Counter({ContactStatus.NEEDS_REVIEW: 40}) and len(audits) == 40
    members = [
        json.loads(m)["contact_id"] for m in redis_client.zrange("recovery:scheduled", 0, -1)
    ]
    assert sorted(members) == sorted(str(c) for c in retry_ids), "each retry scheduled exactly once"
    _assert_ok(redis_client, retry_campaign, fake)


# -- 7. PostgreSQL restarted in the middle of a run ----------------------------------------------


@pytest.mark.slow
def test_postgres_restart_mid_run_recovers_after_reconnect(redis_client, provider, fake):
    """Documented result: while the database is down every loop step fails and rolls back (the
    loop never dies; the unacked job stays pending); the first query after the restart hits a dead
    pooled connection and fails once more, then the pool reconnects and the run completes with one
    dial per contact. Needs P1_PG_RESTART_CMD (restart the server, return when it is back)."""
    command = os.environ.get("P1_PG_RESTART_CMD")
    if not command:
        pytest.skip("set P1_PG_RESTART_CMD to run the PostgreSQL restart scenario")
    campaign_id, ids = _campaign(N)
    assert _run(campaign_id).enqueued == N
    tick = _Tick()
    loop = _loop(redis_client, provider, tick)
    for _ in range(2):
        tick.t += 20
        loop.step()

    restarter = threading.Thread(
        target=lambda: subprocess.run(command, shell=True, check=True, timeout=120)
    )
    restarter.start()
    failures = 0
    while restarter.is_alive():
        tick.t += 20
        if loop.step() is None:  # an exception inside the step was logged and rolled back
            failures += 1
        time.sleep(0.05)
    restarter.join()
    _engine.dispose()  # the test's own pooled connections died with the server

    _drain(loop, tick, idle_stop=15)
    _anchor()
    _complete_all()

    _assert_ok(redis_client, campaign_id, fake, final=True)
    assert max(fake.triggers.values()) == 1 and len(fake.triggers) == N
    assert _statuses(campaign_id) == Counter({ContactStatus.COMPLETED: N})
    print(f"postgres restart: {failures} loop steps failed while the database was down")
