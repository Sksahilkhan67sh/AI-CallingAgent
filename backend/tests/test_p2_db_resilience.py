"""P2-7: the database-resilience gate (blocks Phase 2 completion).

One end-to-end run of the failure modes Phase 2 hardened, against real PostgreSQL and Redis, with
an invariant checker (tests/resilience_invariants.py) so a scenario cannot pass by merely not
crashing. Sizes come from the environment with small CI defaults:

    P2_GATE_JOBS            jobs in the mid-dial kill scenario        (default 4)
    P2_GATE_API_REQUESTS    API requests after a kill                 (default 20)
    P2_GATE_POOL_REQUESTS   concurrent requests against a tiny pool   (default 10)
    P2_PG_RESTART_CMD       shell command that restarts PostgreSQL; unset skips scenario (e)

`ops/resilience/mutation_check.py` proves the gate is not vacuous: removing pre-ping, the
statement timeout, the advisory lock or the 503 mapping must each make it fail."""

import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InternalError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.database import DB_SESSION, make_engine
from app.main import app, create_app
from app.services.admin.auth import AdminPrincipal, create_access_token
from app.services.queue.dialer_worker import JobOutcome
from app.services.queue.job import DialJob
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.resilience_invariants import (
    check_dial_invariants,
    forget_campaign,
    kill_application_connections,
)
from tests.test_failure_injection import (  # noqa: F401  (fixtures and helpers)
    FakeDograh,
    _admission,
    _attempts,
    _drive,
    _pending,
    _queue,
    _Session,
    _world,
)
from tests.test_p2_5_migrate import _alembic, _head, _version, fresh_db  # noqa: F401

DB_URL = make_url(os.environ["PRIMARY_DB_URL"])
BACKEND = Path(__file__).resolve().parents[1]
JOBS = int(os.environ.get("P2_GATE_JOBS", "4"))
API_REQUESTS = int(os.environ.get("P2_GATE_API_REQUESTS", "20"))
POOL_REQUESTS = int(os.environ.get("P2_GATE_POOL_REQUESTS", "10"))
# Phase 1's gate must stay exactly as it was when Phase 2 began; editing it is a decision, so the
# pinned hash has to be updated on purpose.
PHASE1_GATE_SHA256 = "1215caec87d74de5510af3fbddb14b04db039a3f0b61b93d63e05ff4acd01e7e"
_ENV = (
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT",
    "DB_STATEMENT_TIMEOUT_MS",
    "DB_LOCK_TIMEOUT_MS",
    "DB_IDLE_IN_TX_TIMEOUT_MS",
    "API_THREADPOOL_SIZE",
    "WEB_CONCURRENCY",
    "DB_ROLE",
)


def _admin_engine():
    return create_engine(DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")


def _kill_app_connections() -> int:
    admin = _admin_engine()
    try:
        with Session(admin) as s:
            return kill_application_connections(s, f"aca-{get_settings().db_role}")
    finally:
        admin.dispose()


def _unread(queue) -> int:
    group = next(g for g in queue.redis.xinfo_groups(queue.stream_key) if g["name"] == queue.group)
    return int(group.get("lag") or 0)


def _headers() -> dict[str, str]:
    token, _ = create_access_token(AdminPrincipal(username="p2-7", role="admin"))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def world():
    """`_world`, but everything it creates is deleted afterwards."""
    created: list[str] = []

    def make(n: int) -> tuple[str, list[str]]:
        campaign_id, contact_ids = _world(n)
        created.append(campaign_id)
        return campaign_id, contact_ids

    yield make
    with _Session() as s:
        for campaign_id in created:
            forget_campaign(s, campaign_id)


class KillingDograh(FakeDograh):
    """A provider that accepts the call and then loses every database connection (the nastiest
    moment: a real call is ringing and the final commit has nowhere to go)."""

    def __init__(self) -> None:
        super().__init__()
        self.phones: list[str] = []
        self.kill_enabled = True

    def trigger_call(self, *, phone_number: str, initial_context: dict):
        result = super().trigger_call(phone_number=phone_number, initial_context=initial_context)
        self.phones.append(phone_number)
        if self.kill_enabled:
            _kill_app_connections()
        return result


@pytest.fixture
def killing_dograh(monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    fake = KillingDograh()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: fake)
    monkeypatch.setattr("app.services.queue.dialer_worker.time.sleep", lambda _s: None)
    yield fake
    get_settings.cache_clear()


def _worker(queue, admission, provider, redis_client, name: str):
    from app.worker import WorkerLoop

    return WorkerLoop(
        queue=queue,
        admission=admission,
        provider=provider,
        circuit_breaker=CircuitBreaker(redis_client, "dograh"),
        recovery_scheduler=get_recovery_scheduler(),
        consumer_name=name,
        block_ms=200,
        heartbeat=lambda: None,
    )


# == (a) all connections killed mid-run: API, worker, sweeper keep working ===================


def test_a_connection_kill_during_every_dial_loses_no_job_and_dials_each_number_once(
    killing_dograh, redis_client, provider, world
) -> None:
    campaign_id, contact_ids = world(JOBS)
    queue, admission = _queue(redis_client, "p27a"), _admission(redis_client)
    for contact_id in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    worker = _worker(queue, admission, provider, redis_client, "p27-w1")

    for _ in range(JOBS):
        worker.step()  # dials, then loses its connections before the outcome commit

    assert killing_dograh.calls == JOBS  # every call really was placed
    assert _pending(queue) == JOBS  # none acked: nothing may look finished that is not

    with _Session() as s:  # long after any request could still be in flight
        # Relative to the APPLICATION's clock (which faketime moves), not the database's.
        long_ago = datetime.now(UTC) - timedelta(hours=1)
        s.execute(
            text(
                "UPDATE call_attempt SET started_at = :t WHERE contact_id IN "
                "(SELECT id FROM contact WHERE campaign_id = :c)"
            ),
            {"t": long_ago, "c": campaign_id},
        )
        s.commit()
    reclaimed = queue.reclaim_stale("p27-w2", idle_ms=0)
    assert len(reclaimed) == JOBS
    for message in reclaimed:
        assert _drive(queue, admission, provider, redis_client, *message) == (
            JobOutcome.AMBIGUOUS_RECOVERY
        )

    assert killing_dograh.calls == JOBS  # recovery never redialled
    assert _pending(queue) == 0
    with _Session() as s:
        assert (
            check_dial_invariants(
                session=s,
                contact_ids=contact_ids,
                phones_dialed=killing_dograh.phones,
                pending=_pending(queue),
                unread=_unread(queue),
            )
            == []
        )


def test_after_a_kill_the_worker_the_api_and_the_sweeper_keep_working(
    killing_dograh, redis_client, provider, world
) -> None:
    from app.sweeper import sweep_until_clear

    killing_dograh.kill_enabled = False
    campaign_id, contact_ids = world(2)
    queue, admission = _queue(redis_client, "p27b"), _admission(redis_client)
    for contact_id in contact_ids:
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    worker = _worker(queue, admission, provider, redis_client, "p27-w3")
    client = TestClient(app, headers=_headers(), raise_server_exceptions=False)
    assert client.get("/api/v1/campaigns").status_code == 200  # warm the API's pool

    _kill_app_connections()  # every pooled connection of this process is now dead

    statuses = [client.get("/api/v1/campaigns").status_code for _ in range(API_REQUESTS)]
    assert statuses == [200] * API_REQUESTS  # API: no 5xx
    _kill_app_connections()
    sweep_until_clear()  # sweeper: does not raise on the first pass
    _kill_app_connections()
    outcomes = [worker.step(), worker.step()]  # worker: first step after a kill already works
    assert outcomes == [JobOutcome.ADMITTED_AND_DIALED] * 2
    assert killing_dograh.calls == 2 and _pending(queue) == 0


def test_the_invariant_checker_catches_what_it_claims_to_catch() -> None:
    # (The matching positive control is the test above: it feeds the checker string ids against
    # real attempt rows and must report nothing. That is how an id-type bug was caught.)
    with _Session() as s:
        phone = "+919800000001"
        found = check_dial_invariants(
            session=s,
            contact_ids=[str(uuid.uuid4())],  # a contact with no attempt and nothing queued
            phones_dialed=[phone, phone],  # the same number dialled twice
            pending=0,
            unread=0,
        )
    assert any("duplicate dial" in v for v in found), found
    assert any("lost job" in v for v in found), found


# == (b) pool exhaustion -> 503, not a hang ==================================================


def _real_clock_env(base: dict[str, str]) -> dict[str, str]:
    """Environment for a child process that must run on the real clock. libfaketime stalls timed
    waits (the pool timeout, a nested pytest), and these children are the system under test, not
    the code whose time-of-day independence faketime is there to check."""
    env = {k: v for k, v in base.items() if not k.startswith("FAKETIME")}
    if "faketime" in env.get("LD_PRELOAD", ""):
        env.pop("LD_PRELOAD")
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fetch(url: str) -> tuple[int, float, bool]:
    started = time.monotonic()
    with httpx.Client(timeout=60) as client:
        response = client.get(url)
    return response.status_code, time.monotonic() - started, "Retry-After" in response.headers


def test_an_exhausted_pool_answers_503_with_retry_after_and_nothing_hangs(env) -> None:
    port = _free_port()
    child_env = _real_clock_env({k: v for k, v in os.environ.items() if k not in _ENV})
    child_env.update(
        DB_POOL_SIZE="2",
        DB_MAX_OVERFLOW="0",
        DB_POOL_TIMEOUT="1",
        API_THREADPOOL_SIZE="20",  # more request threads than connections, so the pool is the limit
        WEB_CONCURRENCY="1",
    )
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "app.serve", "--app", "tests.p27_app:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=BACKEND,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        url = f"http://127.0.0.1:{port}/_db_slow?seconds=3"
        with ThreadPoolExecutor(max_workers=POOL_REQUESTS) as pool:
            results = list(pool.map(_fetch, [url] * POOL_REQUESTS))
        codes = sorted(code for code, _, _ in results)
        assert codes == [200, 200] + [503] * (POOL_REQUESTS - 2), codes
        assert all(retry for code, _, retry in results if code == 503)
        # Generous: it is ~1 s normally; libfaketime stretches asyncio wake-ups to ~5 s.
        assert max(duration for _, duration, _ in results) < 15
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()


# == (c) statement timeout, lock timeout, idle in transaction ================================


def _limited(env, **limits: str):
    for name, value in limits.items():
        env.setenv(name, value)
    return make_engine(Settings())


def test_a_statement_over_the_limit_is_cancelled_and_the_api_answers_503(env) -> None:
    from app.core import database

    engine = _limited(env, DB_STATEMENT_TIMEOUT_MS="1000")
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError) as caught, engine.connect() as conn:
            conn.execute(text("select pg_sleep(3)"))
        assert type(caught.value.orig).__name__ == "QueryCanceled"
        assert time.monotonic() - started < 2.5

        env.setattr(database, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
        application = create_app()

        @application.get("/_slow")
        def slow(db: Session = DB_SESSION) -> dict[str, bool]:
            db.execute(text("select pg_sleep(3)"))
            return {"ok": True}

        response = TestClient(application, raise_server_exceptions=False).get("/_slow")
        assert response.status_code == 503 and "Retry-After" in response.headers
    finally:
        engine.dispose()


def test_a_row_lock_held_elsewhere_fails_within_the_lock_timeout(env) -> None:
    from app.models.campaign import Campaign
    from app.models.enums import CampaignStatus

    name = f"p2-7-lock-{uuid.uuid4().hex[:8]}"
    admin = create_engine(DB_URL, poolclass=NullPool)
    with Session(admin) as s:
        campaign = Campaign(name=name, status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.commit()
        campaign_id = campaign.id
    holder = admin.connect()
    holder.execute(text("select id from campaign where id = :i for update"), {"i": campaign_id})
    engine = _limited(env, DB_LOCK_TIMEOUT_MS="500")
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError) as caught, engine.connect() as conn:
            conn.execute(text("set local statement_timeout = 10000"))  # safety net, not the test
            conn.execute(
                text("select id from campaign where id = :i for update"), {"i": campaign_id}
            )
        assert type(caught.value.orig).__name__ == "LockNotAvailable"
        assert time.monotonic() - started < 3
    finally:
        engine.dispose()
        holder.rollback()
        holder.close()
        with Session(admin) as s:
            s.execute(text("delete from campaign where name = :n"), {"n": name})
            s.commit()
        admin.dispose()


def test_a_leaked_open_transaction_is_terminated_by_the_server(env) -> None:
    engine = _limited(env, DB_IDLE_IN_TX_TIMEOUT_MS="1000")
    admin = _admin_engine()
    try:
        conn = engine.connect()
        pid = conn.execute(text("select pg_backend_pid()")).scalar_one()
        deadline = time.monotonic() + 15
        alive = 1
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            with admin.connect() as a:
                alive = a.execute(
                    text("select count(*) from pg_stat_activity where pid = :p"), {"p": pid}
                ).scalar_one()
        assert alive == 0
        with pytest.raises((InternalError, OperationalError)):
            conn.execute(text("select 1"))
        conn.close()
    finally:
        engine.dispose()
        admin.dispose()


# == (d) two concurrent migrators ============================================================


def test_two_concurrent_migrators_both_succeed_and_reach_a_single_head(fresh_db: str) -> None:  # noqa: F811
    first, second = _alembic(fresh_db, "upgrade", "head"), _alembic(fresh_db, "upgrade", "head")
    out1, _ = first.communicate(timeout=240)
    out2, _ = second.communicate(timeout=240)
    assert (first.returncode, second.returncode) == (0, 0), out1[-600:] + out2[-600:]
    assert _version(fresh_db) == [_head()]


# == (e) optional: a real PostgreSQL restart ==================================================


@pytest.mark.skipif(
    not os.environ.get("P2_PG_RESTART_CMD"), reason="set P2_PG_RESTART_CMD to restart PostgreSQL"
)
def test_a_real_postgres_restart_is_survived_without_restarting_the_app(redis_client) -> None:
    client = TestClient(app, headers=_headers(), raise_server_exceptions=False)
    assert client.get("/api/v1/campaigns").status_code == 200
    subprocess.run(os.environ["P2_PG_RESTART_CMD"], shell=True, check=True, timeout=120)  # noqa: S602
    deadline = time.monotonic() + 60
    codes: list[int] = []
    while time.monotonic() < deadline:
        code = client.get("/api/v1/campaigns").status_code
        codes.append(code)
        if code == 200:
            break
        time.sleep(0.5)
    assert codes[-1] == 200, codes  # recovered by reconnecting, no app restart
    assert [c for c in codes if c >= 500 and c != 503] == [], codes  # unavailable => 503, never 500
    assert [client.get("/api/v1/campaigns").status_code for _ in range(20)] == [200] * 20


# == (f) the Phase 1 failure-injection gate still passes, unchanged ============================


def test_the_phase_1_gate_is_unchanged_and_still_passes() -> None:
    digest = hashlib.sha256((BACKEND / "tests" / "test_failure_injection.py").read_bytes())
    assert digest.hexdigest() == PHASE1_GATE_SHA256, "the Phase 1 gate was edited"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "tests/test_failure_injection.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=BACKEND,
        env=_real_clock_env(dict(os.environ)),
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-1500:]
