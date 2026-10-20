"""P2-2: the connection pool survives dead connections and fails fast on exhaustion.

Real PostgreSQL throughout. The kill tests terminate exactly the backends the application's own
pool holds (never other tests' connections). Timings use generous bounds (seconds, not
milliseconds) and nothing depends on the time of day."""

import asyncio
import os
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DisconnectionError, OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.database import DB_SESSION
from app.main import app, create_app
from app.services.admin.auth import AdminPrincipal, create_access_token
from app.services.queue.dialer_worker import JobOutcome
from app.services.queue.job import DialJob
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from tests.test_failure_injection import (  # noqa: F401  (dograh is a fixture)
    _admission,
    _attempts,
    _pending,
    _queue,
    _world,
    dograh,
)

DB_URL = os.environ["PRIMARY_DB_URL"]
# libfaketime distorts asyncio wake-ups: the same 1 s pool timeout that fires at 1.0 s in a plain
# run takes ~5 s to reach the client through an event loop (measured). The behaviour is still
# covered under faketime by the thread-based engine test below and by the handler tests.
_UNDER_FAKETIME = "faketime" in os.environ.get("LD_PRELOAD", "")
_POOL_ENV = ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE")


def _admin_engine():
    return create_engine(DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")


def _pooled_pids(engine, n: int) -> list[int]:
    """Backend pids of `n` connections held by `engine`'s pool (opened together, then returned)."""
    conns = [engine.connect() for _ in range(n)]
    try:
        return [c.execute(text("select pg_backend_pid()")).scalar_one() for c in conns]
    finally:
        for c in conns:
            c.close()


def _kill(pids: list[int]) -> None:
    admin = _admin_engine()
    with admin.connect() as c:
        for pid in pids:
            c.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            alive = c.execute(
                text("select count(*) from pg_stat_activity where pid = any(:p)"), {"p": pids}
            ).scalar_one()
            if alive == 0:
                break
            time.sleep(0.05)
    admin.dispose()


def _headers() -> dict[str, str]:
    token, _ = create_access_token(AdminPrincipal(username="p2-2", role="admin"))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pool_env(monkeypatch):
    for name in _POOL_ENV + ("DB_POOL_PRE_PING", "DB_RETRY_AFTER_SECONDS", "PRIMARY_DB_POOL_SIZE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# -- settings -----------------------------------------------------------------


def test_pool_settings_have_safe_defaults(pool_env) -> None:
    s = Settings()
    assert (s.db_pool_size, s.db_max_overflow) == (5, 10)
    assert s.db_pool_timeout == 10
    assert s.db_pool_recycle == 1800
    assert s.db_pool_pre_ping is True
    assert s.db_retry_after_seconds == 2


def test_pool_settings_read_the_environment_and_keep_the_legacy_pool_size_name(pool_env) -> None:
    pool_env.setenv("PRIMARY_DB_POOL_SIZE", "7")
    assert Settings().db_pool_size == 7  # documented name before Phase 2
    pool_env.setenv("DB_POOL_SIZE", "3")
    pool_env.setenv("DB_MAX_OVERFLOW", "0")
    pool_env.setenv("DB_POOL_TIMEOUT", "1.5")
    pool_env.setenv("DB_POOL_RECYCLE", "0")
    pool_env.setenv("DB_POOL_PRE_PING", "false")
    s = Settings()
    assert (s.db_pool_size, s.db_max_overflow, s.db_pool_timeout) == (3, 0, 1.5)
    assert s.db_pool_recycle == 0 and s.db_pool_pre_ping is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_POOL_SIZE", "0"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DB_POOL_TIMEOUT", "0"),
        ("DB_POOL_RECYCLE", "-5"),
        ("DB_RETRY_AFTER_SECONDS", "0"),
    ],
)
def test_nonsense_pool_settings_are_rejected_at_boot(pool_env, name: str, value: str) -> None:
    pool_env.setenv(name, value)
    with pytest.raises(ValueError):  # noqa: PT011  (pydantic ValidationError subclasses it)
        Settings()


def test_the_engine_is_built_from_the_settings(pool_env) -> None:
    from app.core.database import make_engine

    pool_env.setenv("DB_POOL_SIZE", "3")
    pool_env.setenv("DB_MAX_OVERFLOW", "4")
    pool_env.setenv("DB_POOL_TIMEOUT", "2.5")
    pool_env.setenv("DB_POOL_RECYCLE", "60")
    engine = make_engine(Settings())
    try:
        pool = engine.pool
        assert (pool.size(), pool._max_overflow, pool._timeout) == (3, 4, 2.5)  # noqa: SLF001
        assert pool._recycle == 60 and pool._pre_ping is True  # noqa: SLF001
    finally:
        engine.dispose()


def test_the_module_engine_uses_pre_ping() -> None:
    from app.core.database import engine

    assert engine.pool._pre_ping is True  # noqa: SLF001


# -- API boundary: 503 + Retry-After, never a 500 and never a leak -----------------


def _boom_app(exc: Exception):
    application = create_app()

    @application.get("/_boom")
    def boom() -> None:
        raise exc

    return TestClient(application, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("SELECT secret_column", {"p": "hunter2"}, Exception("host=db.internal")),
        DisconnectionError("connection invalidated for postgresql://user:hunter2@db.internal/x"),
        PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached"),
    ],
    ids=["operational", "disconnection", "pool-timeout"],
)
def test_database_unavailability_is_a_503_with_retry_after_and_no_internals(exc) -> None:
    response = _boom_app(exc).get("/_boom")
    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) == get_settings().db_retry_after_seconds
    body = response.text
    for leaked in ("hunter2", "db.internal", "secret_column", "QueuePool", "postgresql"):
        assert leaked not in body


def test_other_database_errors_are_not_disguised_as_unavailability() -> None:
    # A programming error is a bug (500), not something a client should retry.
    response = _boom_app(ProgrammingError("SELECT 1", {}, Exception("syntax"))).get("/_boom")
    assert response.status_code == 500


# -- accept (1): kill every pooled connection, then 50 requests -> 0 5xx -------------


def test_killing_every_pooled_connection_causes_no_5xx(redis_client) -> None:
    from app.core.database import engine

    client = TestClient(app, headers=_headers(), raise_server_exceptions=False)
    assert client.get("/api/v1/campaigns").status_code == 200  # warm the pool
    _kill(_pooled_pids(engine, 4))
    statuses = [client.get("/api/v1/campaigns").status_code for _ in range(50)]
    assert statuses == [200] * 50


# -- accept (2): exhausted pool -> surplus gets 503 within ~timeout + 1 s, none hangs -------


def test_pool_timeout_fires_on_the_configured_engine(pool_env) -> None:
    """Engine level, no event loop (so it also runs under faketime): with 2 connections busy for
    3 s, the other requests get the pool timeout after ~1 s instead of waiting for them."""
    from app.core import database

    pool_env.setenv("DB_POOL_SIZE", "2")
    pool_env.setenv("DB_MAX_OVERFLOW", "0")
    pool_env.setenv("DB_POOL_TIMEOUT", "1")
    engine = database.make_engine(Settings())
    results: list[tuple[str, float]] = []

    def work() -> None:
        started = time.monotonic()
        try:
            with engine.connect() as conn:
                conn.execute(text("select pg_sleep(3)"))
            results.append(("ok", time.monotonic() - started))
        except PoolTimeoutError:
            results.append(("timeout", time.monotonic() - started))

    threads = [threading.Thread(target=work) for _ in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        engine.dispose()
    kinds = sorted(kind for kind, _ in results)
    assert kinds == ["ok", "ok", "timeout", "timeout", "timeout", "timeout"]
    assert all(duration < 2.5 for kind, duration in results if kind == "timeout"), results


@pytest.mark.skipif(_UNDER_FAKETIME, reason="libfaketime distorts asyncio wake-ups (see above)")
def test_pool_exhaustion_returns_503_quickly_instead_of_hanging(pool_env) -> None:
    from app.core import database

    pool_env.setenv("DB_POOL_SIZE", "2")
    pool_env.setenv("DB_MAX_OVERFLOW", "0")
    pool_env.setenv("DB_POOL_TIMEOUT", "1")
    get_settings.cache_clear()
    engine = database.make_engine(get_settings())
    pool_env.setattr(database, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    application = create_app()

    @application.get("/_slow")
    def slow(db: Session = DB_SESSION) -> dict[str, bool]:
        db.execute(text("select pg_sleep(3)"))
        return {"ok": True}

    async def fire() -> list[tuple[int, float, httpx.Response]]:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:

            async def one() -> tuple[int, float, httpx.Response]:
                started = time.monotonic()
                response = await c.get("/_slow", timeout=30)
                return response.status_code, time.monotonic() - started, response

            return await asyncio.gather(*[one() for _ in range(10)])

    try:
        results = asyncio.run(fire())
    finally:
        engine.dispose()
    ok = [r for r in results if r[0] == 200]
    shed = [r for r in results if r[0] == 503]
    assert (len(ok), len(shed)) == (2, 8), [r[0] for r in results]
    assert all(duration < 2.5 for _, duration, _ in shed), [round(r[1], 1) for r in shed]
    assert all("Retry-After" in r.headers for _, _, r in shed)


# -- accept (3): a worker mid-run survives the kill and processes the job exactly once ------


def test_worker_survives_killed_connections_and_dials_exactly_once(
    dograh, redis_client, provider  # noqa: F811
) -> None:
    from app.core.database import engine
    from app.worker import WorkerLoop

    campaign_id, (contact_id,) = _world()
    queue, admission = _queue(redis_client, "p22"), _admission(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1))
    worker = WorkerLoop(
        queue=queue,
        admission=admission,
        provider=provider,
        circuit_breaker=CircuitBreaker(redis_client, "dograh"),
        recovery_scheduler=get_recovery_scheduler(),
        consumer_name="p22-w1",
        block_ms=200,
        heartbeat=lambda: None,
    )
    _kill(_pooled_pids(engine, 4))  # the worker's own pool is now full of dead connections

    assert worker.step() == JobOutcome.ADMITTED_AND_DIALED  # first step, no retry needed
    assert _pending(queue) == 0
    assert dograh.calls == 1
    assert len(_attempts(contact_id)) == 1
