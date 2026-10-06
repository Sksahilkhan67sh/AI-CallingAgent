"""CP12-A -- concurrency leases (slot-leak protection), against a REAL Redis.

Time is deterministic: `FakeClock` is injected as the lease clock, so expiry, renewal and
crash recovery are tested by moving the clock, never by sleeping. One test
(`test_real_redis_clock_expires_a_lease`) deliberately uses Redis' own TIME with a 1-second
lease to prove the production time source works too -- it is the only test that waits.

What these prove: ownership safety, atomicity, expiry-based recovery and fail-closed
behaviour of the lease store / AdmissionController. What they do NOT prove: behaviour of a
real Dograh call, or throughput at 100K contacts (see docs/CHECKPOINT-12A-NOTES.md).
"""

import random
import threading
import uuid

import pytest
import redis as redis_lib

from app.core.config import Settings
from app.services.queue import dialer_worker
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.concurrency_lease import REASON_DUPLICATE, ConcurrencyLease
from app.services.queue.dialer_worker import JobOutcome, process_claimed_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker

GLOBAL_KEY = "concurrency:lease:global"
TTL_S = 30
_BIG = 10**9  # CPS limits that never interfere with concurrency tests


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = 1_700_000_000_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _controller(
    redis_client, clock, *, g=100, c=100, p=100, ttl=TTL_S, worker="worker-a", cps=_BIG
) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=cps,
        campaign_cps_limit=cps,
        provider_cps_limit=cps,
        global_concurrency_limit=g,
        campaign_concurrency_limit=c,
        provider_concurrency_limit=p,
        lease_ttl_seconds=ttl,
        worker_id=worker,
        clock=clock,
    )


def _admit(ctrl, *, campaign="c1", provider="mock", holder=None):
    return ctrl.try_admit(campaign_id=campaign, provider_name=provider, holder_id=holder)


def _active(redis_client, key=GLOBAL_KEY) -> int:
    return redis_client.zcard(key)


# -- A. basic acquire ------------------------------------------------------------------------------


def test_acquire_succeeds_with_capacity_and_is_rejected_when_exhausted(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=2)
    first, second, third = (_admit(ctrl, campaign=f"c{i}") for i in range(3))
    assert first.admitted and second.admitted
    assert not third.admitted and third.reason == "concurrency_exceeded"
    assert third.lease is None
    assert _active(redis_client) == 2


@pytest.mark.parametrize(
    ("limits", "second_args"),
    [
        ({"g": 1}, {"campaign": "other", "provider": "other"}),
        ({"c": 1}, {"campaign": "c1", "provider": "other"}),
        ({"p": 1}, {"campaign": "other", "provider": "mock"}),
    ],
)
def test_each_scope_limit_is_enforced_independently(redis_client, clock, limits, second_args):
    ctrl = _controller(redis_client, clock, **limits)
    assert _admit(ctrl).admitted
    assert _admit(ctrl, **second_args).reason == "concurrency_exceeded"


# -- B. atomic race --------------------------------------------------------------------------------


def test_race_for_the_last_slot_has_exactly_one_owner(redis_client, clock):
    n = 32
    ctrl = _controller(redis_client, clock, g=1)
    barrier = threading.Barrier(n)
    results: list[bool] = []

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        results.append(_admit(ctrl, campaign=f"c{i}", holder=f"h{i}").admitted)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert _active(redis_client) == 1


# -- C. release ------------------------------------------------------------------------------------


def test_release_frees_the_slot_for_another_worker(redis_client, clock):
    ctrl_a = _controller(redis_client, clock, g=1, worker="a")
    ctrl_b = _controller(redis_client, clock, g=1, worker="b")
    a = _admit(ctrl_a, holder="h1")
    assert not _admit(ctrl_b, holder="h2").admitted
    assert ctrl_a.release(a.lease) is True
    assert _admit(ctrl_b, holder="h2").admitted


# -- D. double release -----------------------------------------------------------------------------


def test_double_release_is_a_noop_and_never_corrupts_capacity(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=2)
    a = _admit(ctrl, holder="h1")
    b = _admit(ctrl, holder="h2")
    assert ctrl.release(a.lease) is True
    assert ctrl.release(a.lease) is False  # second release: no-op
    assert ctrl.release(a.lease) is False
    assert _active(redis_client) == 1  # B's lease untouched; count never negative
    assert redis_client.zscore(GLOBAL_KEY, b.lease.lease_id) is not None
    assert _admit(ctrl, holder="h3").admitted  # exactly one slot was free, not two
    assert not _admit(ctrl, holder="h4").admitted


# -- E. ownership safety ---------------------------------------------------------------------------


def test_a_forged_lease_cannot_release_someone_elses_slot(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    a = _admit(ctrl, holder="h1")
    forged = ConcurrencyLease(uuid.uuid4().hex, "h1", "c1", "mock")  # right slot, wrong token
    assert ctrl.release(forged) is False
    assert redis_client.zscore(GLOBAL_KEY, a.lease.lease_id) is not None  # A still owns it
    assert not _admit(ctrl, holder="h2").admitted
    assert redis_client.exists("concurrency:holder:h1") == 1  # idempotency marker intact


# -- F / G. renewal --------------------------------------------------------------------------------


def test_renew_extends_the_expiry(redis_client, clock):
    ctrl = _controller(redis_client, clock)
    lease = _admit(ctrl).lease
    original = redis_client.zscore(GLOBAL_KEY, lease.lease_id)
    clock.advance(TTL_S - 5)
    assert ctrl.leases.renew(lease) is True
    renewed = redis_client.zscore(GLOBAL_KEY, lease.lease_id)
    assert renewed == original + (TTL_S - 5) * 1000
    clock.advance(10)  # past the ORIGINAL expiry, inside the renewed one
    assert _active_after_reclaim(ctrl, redis_client, clock) == 1


def test_renewal_by_a_non_owner_is_rejected_and_changes_nothing(redis_client, clock):
    ctrl = _controller(redis_client, clock)
    lease = _admit(ctrl).lease
    before = redis_client.zscore(GLOBAL_KEY, lease.lease_id)
    clock.advance(5)
    forged = ConcurrencyLease(uuid.uuid4().hex, lease.holder_id, "c1", "mock")
    assert ctrl.leases.renew(forged) is False
    assert redis_client.zscore(GLOBAL_KEY, lease.lease_id) == before
    assert redis_client.zcard(GLOBAL_KEY) == 1  # nothing was created either


def _active_after_reclaim(ctrl, redis_client, clock) -> int:
    """Force the expired-lease cleanup (it runs inside acquire) and count what is left."""
    _admit(ctrl, campaign="probe", holder=f"probe-{uuid.uuid4().hex}")
    return redis_client.zcard(GLOBAL_KEY) - 1  # minus the probe's own lease


# -- H. expiration ---------------------------------------------------------------------------------


def test_expired_lease_returns_its_capacity(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    assert _admit(ctrl, holder="h1").admitted
    clock.advance(TTL_S - 1)
    assert not _admit(ctrl, holder="h2").admitted  # still live
    clock.advance(1)  # exactly at expiry: no longer live
    assert _admit(ctrl, holder="h2").admitted
    assert _active(redis_client) == 1


# -- I. crash simulation ---------------------------------------------------------------------------


def test_killed_worker_does_not_leak_capacity_permanently(redis_client, clock):
    worker_a = _controller(redis_client, clock, g=1, worker="worker-a")
    worker_b = _controller(redis_client, clock, g=1, worker="worker-b")
    crashed = _admit(worker_a, holder="h1")
    assert crashed.admitted  # ...and then worker A is SIGKILLed: release() is never called
    assert not _admit(worker_b, holder="h2").admitted

    clock.advance(TTL_S)  # no manual counter reset, no janitor: time alone recovers the slot
    recovered = _admit(worker_b, holder="h2")
    assert recovered.admitted
    assert _active(redis_client) == 1
    assert redis_client.zscore(GLOBAL_KEY, crashed.lease.lease_id) is None


# -- J / K. delayed release and renewal from a worker whose lease expired --------------------------


def test_delayed_release_from_an_expired_owner_does_not_touch_the_new_owner(redis_client, clock):
    a = _admit(_controller(redis_client, clock, g=1, worker="a"), holder="h1")
    ctrl_b = _controller(redis_client, clock, g=1, worker="b")
    clock.advance(TTL_S)
    b = _admit(ctrl_b, holder="h2")
    assert b.admitted

    assert ctrl_b.release(a.lease) is False  # A's late release, even from the same holder id
    assert redis_client.zscore(GLOBAL_KEY, b.lease.lease_id) is not None
    assert not _admit(ctrl_b, holder="h3").admitted  # B still owns the only slot


def test_delayed_release_for_the_same_attempt_keeps_the_new_leases_marker(redis_client, clock):
    """A's lease expired and the SAME logical attempt (holder) was re-admitted as lease B."""
    ctrl = _controller(redis_client, clock, g=1)
    a = _admit(ctrl, holder="same-attempt")
    clock.advance(TTL_S)
    b = _admit(ctrl, holder="same-attempt")
    assert b.admitted
    assert ctrl.release(a.lease) is False
    assert redis_client.get("concurrency:holder:same-attempt").startswith(b.lease.lease_id)
    assert _admit(ctrl, holder="same-attempt").reason == REASON_DUPLICATE  # still deduplicated


def test_delayed_renewal_from_an_expired_owner_cannot_resurrect_or_steal(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    a = _admit(ctrl, holder="h1")
    clock.advance(TTL_S)
    b = _admit(ctrl, holder="h2")
    assert b.admitted

    assert ctrl.leases.renew(a.lease) is False
    assert redis_client.zscore(GLOBAL_KEY, a.lease.lease_id) is None  # A not resurrected
    assert redis_client.zscore(GLOBAL_KEY, b.lease.lease_id) is not None
    assert _active(redis_client) == 1


def test_expired_lease_is_dead_even_if_nobody_has_reclaimed_it_yet(redis_client, clock):
    """Renewability must not depend on whether another worker happened to run the cleanup."""
    ctrl = _controller(redis_client, clock, g=1)
    lease = _admit(ctrl, holder="h1").lease
    expiry = redis_client.zscore(GLOBAL_KEY, lease.lease_id)
    clock.advance(TTL_S)  # expired; the entry is still physically in Redis (no acquire ran)
    assert redis_client.zscore(GLOBAL_KEY, lease.lease_id) == expiry
    assert ctrl.leases.renew(lease) is False
    assert redis_client.zscore(GLOBAL_KEY, lease.lease_id) == expiry  # expiry not extended
    assert _admit(ctrl, holder="h2").admitted  # and the slot really is reclaimable


def test_renewal_after_release_cannot_resurrect_the_lease(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    a = _admit(ctrl, holder="h1")
    ctrl.release(a.lease)
    assert ctrl.leases.renew(a.lease) is False
    assert _active(redis_client) == 0
    assert _admit(ctrl, holder="h2").admitted


def _race_renew_vs_acquire(ctrl, lease, other_holder) -> dict[str, bool]:
    barrier = threading.Barrier(2)
    out: dict[str, bool] = {}

    def renew():
        barrier.wait(timeout=5)
        out["renewed"] = ctrl.leases.renew(lease)

    def take():
        barrier.wait(timeout=5)
        out["taken"] = _admit(ctrl, campaign="other", holder=other_holder).admitted

    threads = [threading.Thread(target=renew), threading.Thread(target=take)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def test_renewal_expiry_boundary_race_never_yields_two_owners(redis_client, clock):
    """A renews at the instant B tries to take the slot. Redis runs the two scripts one after
    the other, so exactly one of them wins -- whichever order, never both."""
    rng = random.Random(12)
    ctrl = _controller(redis_client, clock, g=1)
    for round_no in range(60):
        redis_client.flushdb()
        a = _admit(ctrl, holder=f"a{round_no}")
        expiry = clock.now_ms + TTL_S * 1000
        clock.now_ms = expiry + rng.choice([-2, -1, 0, 1, 2])  # straddle the boundary
        out = _race_renew_vs_acquire(ctrl, a.lease, f"b{round_no}")
        assert out["renewed"] != out["taken"], (round_no, out)  # exactly one owner
        assert _active(redis_client) == 1


# -- L. multi-scope rollback -----------------------------------------------------------------------


def test_rejection_in_a_later_scope_leaves_no_partial_capacity(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=10, c=1, p=10)
    assert _admit(ctrl, campaign="c1", holder="h1").admitted
    rejected = _admit(ctrl, campaign="c1", holder="h2")  # global free, campaign full
    assert rejected.reason == "concurrency_exceeded"
    assert _active(redis_client) == 1  # the global slot was NOT taken by the failed attempt
    assert _active(redis_client, "concurrency:lease:provider:mock") == 1
    assert redis_client.exists("concurrency:holder:h2") == 0


def test_provider_scope_rejection_leaves_global_and_campaign_untouched(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=10, c=10, p=1)
    assert _admit(ctrl, campaign="c1", holder="h1").admitted
    assert not _admit(ctrl, campaign="c2", holder="h2").admitted  # provider full
    assert _active(redis_client) == 1
    assert _active(redis_client, "concurrency:lease:campaign:c2") == 0


def test_cps_rejection_releases_the_lease_it_just_took(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=10, cps=1)
    assert _admit(ctrl, holder="h1").admitted  # uses this second's CPS budget
    over = _admit(ctrl, holder="h2")
    assert over.reason == "cps_exceeded"
    assert _active(redis_client) == 1  # only h1 holds a lease
    assert redis_client.exists("concurrency:holder:h2") == 0


# -- M. Redis failure ------------------------------------------------------------------------------


class _FailingScript:
    def __init__(self, exc: Exception) -> None:
        self.exc, self.calls = exc, 0

    def __call__(self, **_kwargs):
        self.calls += 1
        raise self.exc


def test_redis_timeout_during_acquire_fails_closed_with_no_in_memory_fallback(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    acquire = _FailingScript(redis_lib.TimeoutError("timed out"))
    ctrl.leases.__dict__["_acquire"] = acquire  # replaces the cached script handle
    with pytest.raises(redis_lib.TimeoutError):
        _admit(ctrl, holder="h1")  # no AdmissionResult(admitted=True) is ever produced
    assert acquire.calls == 1
    # an ambiguous acquire (the reply may have been lost) is followed by a best-effort release
    del ctrl.leases.__dict__["_acquire"]
    assert _active(redis_client) == 0
    assert _admit(ctrl, holder="h1").admitted  # and the same attempt is not locked out


def test_ambiguous_acquire_is_cleaned_up_when_the_reply_is_lost(redis_client, clock):
    """The script ran, then the reply was lost: the lease must not sit there until its TTL."""
    ctrl = _controller(redis_client, clock, g=1)
    real_acquire = ctrl.leases._acquire

    def run_then_lose_reply(**kwargs):
        real_acquire(**kwargs)
        raise redis_lib.ConnectionError("reply lost")

    ctrl.leases.__dict__["_acquire"] = run_then_lose_reply
    with pytest.raises(redis_lib.ConnectionError):
        _admit(ctrl, holder="h1")
    assert _active(redis_client) == 0
    assert redis_client.exists("concurrency:holder:h1") == 0


def test_redis_error_after_acquire_releases_the_lease(redis_client, clock, monkeypatch):
    ctrl = _controller(redis_client, clock, g=1)

    def boom(*_a, **_k):
        raise redis_lib.ConnectionError("down during CPS")

    monkeypatch.setattr(redis_client, "incr", boom)
    with pytest.raises(redis_lib.ConnectionError):
        _admit(ctrl, holder="h1")
    assert _active(redis_client) == 0  # the slot is not held for a call that will not happen


def test_release_never_raises_when_redis_is_down_and_the_ttl_is_the_backstop(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    lease = _admit(ctrl, holder="h1").lease
    good_release = ctrl.leases._release
    ctrl.leases.__dict__["_release"] = _FailingScript(redis_lib.ConnectionError("down"))
    assert ctrl.release(lease) is False  # swallowed + logged: must not mask the call's outcome
    assert _active(redis_client) == 1  # still held ...
    ctrl.leases.__dict__["_release"] = good_release
    clock.advance(TTL_S)
    assert _admit(ctrl, holder="h2").admitted  # ... until the TTL reclaims it


def test_circuit_breaker_open_rejects_without_touching_leases(redis_client, clock):
    ctrl = _controller(redis_client, clock)
    breaker = CircuitBreaker(redis_client, "mock")
    for _ in range(50):
        breaker.record_failure()
    assert _admit(ctrl).reason == "circuit_open"
    assert _active(redis_client) == 0


# -- N. worker exception / lifecycle ---------------------------------------------------------------


@pytest.fixture
def dial_env(redis_client, provider, db_session, monkeypatch):
    queue = RedisStreamQueue(redis_client, "test:cp12a:q", "test:cp12a:g")
    ctrl = _controller(redis_client, FakeClock(), g=5)
    job = DialJob.new(campaign_id=uuid.uuid4(), contact_id=uuid.uuid4(), attempt_number=1)
    breaker = CircuitBreaker(redis_client, provider.name)
    monkeypatch.setattr(dialer_worker.kill_switch, "block_reason", lambda: None)

    def run():
        return process_claimed_job(db_session, queue, ctrl, provider, breaker, "0-1", job)

    return run, job


def test_lease_is_held_during_the_dial_and_released_after(dial_env, redis_client, monkeypatch):
    run, job = dial_env
    seen: dict[str, int] = {}

    def fake_dial(*_args):
        seen["active"] = _active(redis_client)
        seen["holder"] = redis_client.exists(f"concurrency:holder:{job.idempotency_key}")
        return JobOutcome.ADMITTED_AND_DIALED

    monkeypatch.setattr(dialer_worker, "_dial", fake_dial)
    assert run() == JobOutcome.ADMITTED_AND_DIALED
    assert seen == {"active": 1, "holder": 1}  # keyed by the attempt's idempotency key
    assert _active(redis_client) == 0
    assert redis_client.exists(f"concurrency:holder:{job.idempotency_key}") == 0


def test_exception_inside_the_dial_still_releases_the_lease(dial_env, redis_client, monkeypatch):
    run, job = dial_env

    def exploding_dial(*_args):
        raise RuntimeError("python exception after acquisition")

    monkeypatch.setattr(dialer_worker, "_dial", exploding_dial)
    with pytest.raises(RuntimeError):
        run()
    assert _active(redis_client) == 0  # `finally` cleanup ran; the TTL is only the second net


def test_unreleasable_lease_after_exception_is_still_bounded_by_the_ttl(redis_client, clock):
    """If even the cleanup cannot reach Redis (the situation a `finally` cannot cover)."""
    ctrl = _controller(redis_client, clock, g=1)
    lease = _admit(ctrl, holder="h1").lease
    ctrl.leases.__dict__["_release"] = _FailingScript(redis_lib.ConnectionError("down"))
    ctrl.release(lease)
    ttl_ms = redis_client.pttl("concurrency:holder:h1")
    assert 0 < ttl_ms <= TTL_S * 1000  # finite, never immortal


# -- O. duplicate acquisition ----------------------------------------------------------------------


def test_same_attempt_cannot_take_a_second_slot(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=5)
    first = _admit(ctrl, holder="attempt-1")
    second = _admit(ctrl, holder="attempt-1")
    assert first.admitted
    assert not second.admitted and second.reason == REASON_DUPLICATE
    assert _active(redis_client) == 1  # exactly one slot consumed in every scope
    assert _active(redis_client, "concurrency:lease:campaign:c1") == 1
    ctrl.release(first.lease)
    assert _admit(ctrl, holder="attempt-1").admitted  # a finished attempt may be admitted again


def test_duplicate_marker_of_a_crashed_attempt_expires_with_its_lease(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=5)
    assert _admit(ctrl, holder="attempt-1").admitted  # worker dies holding it
    assert _admit(ctrl, holder="attempt-1").reason == REASON_DUPLICATE
    clock.advance(TTL_S)
    assert _admit(ctrl, holder="attempt-1").admitted  # the redelivered job is admitted again
    assert _active(redis_client) == 1


# -- Redis behaviour the design relies on ----------------------------------------------------------


def test_every_lease_key_has_a_finite_ttl(redis_client, clock):
    ctrl = _controller(redis_client, clock)
    lease = _admit(ctrl, holder="h1").lease
    keys = [*ctrl.leases.scope_keys("c1", "mock"), "concurrency:holder:h1"]
    for key in keys:
        assert 0 < redis_client.pttl(key) <= TTL_S * 1000, key
    clock.advance(1)
    assert ctrl.leases.renew(lease)
    for key in keys:
        assert 0 < redis_client.pttl(key) <= TTL_S * 1000, key  # renewal never makes it immortal


def test_holder_record_is_diagnosable_and_has_no_secrets(redis_client, clock):
    ctrl = _controller(redis_client, clock, worker="host|1")
    lease = _admit(ctrl, holder="h1").lease
    lease_id, expires, acquired, worker = redis_client.get("concurrency:holder:h1").split("|")
    assert lease_id == lease.lease_id and worker == "host_1"
    assert int(expires) - int(acquired) == TTL_S * 1000
    assert len(lease_id) == 32  # 128 random bits


def test_scripts_recover_after_script_flush_and_data_loss(redis_client, clock):
    ctrl = _controller(redis_client, clock, g=1)
    first = _admit(ctrl, holder="h1")
    assert first.admitted
    redis_client.script_flush()  # e.g. a Redis restart: cached script SHAs are gone
    assert ctrl.release(first.lease) is True  # redis-py reloads the script transparently
    second = _admit(ctrl, holder="h2")
    assert second.admitted
    redis_client.flushdb()  # Redis lost its data: capacity is free again, never stuck
    assert _admit(ctrl, holder="h3").admitted


def test_real_redis_clock_expires_a_lease(redis_client):
    ctrl = AdmissionController(
        redis_client,
        global_cps_limit=_BIG,
        campaign_cps_limit=_BIG,
        provider_cps_limit=_BIG,
        global_concurrency_limit=1,
        campaign_concurrency_limit=1,
        provider_concurrency_limit=1,
        lease_ttl_seconds=1,
        worker_id="real-clock",
    )
    assert _admit(ctrl, holder="h1").admitted
    assert not _admit(ctrl, holder="h2").admitted
    threading.Event().wait(1.2)  # the only real wait in this module
    assert _admit(ctrl, holder="h2").admitted


# -- configuration ---------------------------------------------------------------------------------


def test_lease_ttl_must_outlast_the_longest_dial():
    def settings(**overrides) -> Settings:
        return Settings(_env_file=None, **overrides)

    with pytest.raises(ValueError, match="CONCURRENCY_LEASE_TTL_SECONDS"):
        settings(concurrency_lease_ttl_seconds=60)  # 3 x (5s + 15s) = 60s: not enough
    assert settings(concurrency_lease_ttl_seconds=61).concurrency_lease_ttl_seconds == 61
    assert settings().concurrency_lease_ttl_seconds == 120


def test_a_zero_ttl_lease_is_refused(redis_client, clock):
    with pytest.raises(ValueError):
        _controller(redis_client, clock, ttl=0)


# -- P. stress -------------------------------------------------------------------------------------


def test_stress_never_exceeds_the_limit_and_leaks_nothing(redis_client, clock):
    limit, n_workers, rounds = 5, 24, 60
    ctrl = _controller(redis_client, clock, g=limit, c=limit, p=limit)
    guard = threading.Lock()
    held: set[str] = set()
    peak = {"held": 0, "violations": 0, "duplicates": 0, "redis_peak": 0}
    stop = threading.Event()

    def sampler():  # observes Redis directly while the workers hammer it
        while not stop.is_set():
            peak["redis_peak"] = max(peak["redis_peak"], _active(redis_client))

    def worker(i: int) -> None:
        for r in range(rounds):
            result = _admit(ctrl, holder=f"w{i}-r{r}")
            if not result.admitted:
                continue
            with guard:
                if result.lease.lease_id in held:
                    peak["duplicates"] += 1
                held.add(result.lease.lease_id)
                peak["held"] = max(peak["held"], len(held))
                if len(held) > limit:
                    peak["violations"] += 1
            with guard:
                held.discard(result.lease.lease_id)
            assert ctrl.release(result.lease) is True

    sampler_thread = threading.Thread(target=sampler)
    sampler_thread.start()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    sampler_thread.join()

    assert peak["violations"] == 0 and peak["duplicates"] == 0
    assert peak["held"] <= limit and peak["redis_peak"] <= limit
    assert _active(redis_client) == 0  # nothing leaked; no negative or phantom capacity
    assert redis_client.keys("concurrency:holder:*") == []


def test_stress_with_crashing_workers_recovers_every_leaked_slot(redis_client, clock):
    limit = 6
    ctrl = _controller(redis_client, clock, g=limit, c=limit, p=limit)
    crashed: list[ConcurrencyLease] = []
    guard = threading.Lock()

    def worker(i: int) -> None:
        rng = random.Random(i)
        for r in range(40):
            result = _admit(ctrl, holder=f"w{i}-r{r}")
            if not result.admitted:
                continue
            if rng.random() < 0.3:  # "SIGKILL": no release ever happens
                with guard:
                    crashed.append(result.lease)
            else:
                ctrl.release(result.lease)
            assert _active(redis_client) <= limit

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert crashed  # the scenario really leaked leases
    clock.advance(TTL_S)  # every crashed worker's lease is now expired
    fresh = [_admit(ctrl, holder=f"fresh-{i}") for i in range(limit)]
    assert all(f.admitted for f in fresh)  # the FULL capacity is back, with no manual reset
    assert not _admit(ctrl, holder="one-too-many").admitted
    assert _active(redis_client) == limit  # and none of the dead leases linger
