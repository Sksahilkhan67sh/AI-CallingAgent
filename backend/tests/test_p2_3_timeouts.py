"""P2-3: server-side statement, lock and idle-in-transaction timeouts, per process role.

Real PostgreSQL. The server enforces every limit, so none of these depends on the Python clock
beyond coarse upper bounds (seconds)."""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InternalError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.database import DB_SESSION, make_engine
from app.main import create_app

DB_URL = os.environ["PRIMARY_DB_URL"]
BACKEND = Path(__file__).resolve().parents[1]
_ENV_NAMES = (
    "DB_ROLE",
    "DB_STATEMENT_TIMEOUT_MS",
    "DB_LOCK_TIMEOUT_MS",
    "DB_IDLE_IN_TX_TIMEOUT_MS",
    "DB_WORKER_STATEMENT_TIMEOUT_MS",
    "DB_WORKER_LOCK_TIMEOUT_MS",
    "DB_WORKER_IDLE_IN_TX_TIMEOUT_MS",
    "DB_MIGRATION_LOCK_TIMEOUT_MS",
    "JOB_DEADLINE_SECONDS",
)
# libfaketime stretches asyncio wake-ups (see test_p2_2_pool_resilience); the server-side
# behaviour is covered by the synchronous engine tests, which do run under faketime.
_UNDER_FAKETIME = "faketime" in os.environ.get("LD_PRELOAD", "")


@pytest.fixture
def env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _show(engine, setting: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text(f"show {setting}")).scalar_one()


# -- settings -----------------------------------------------------------------


def test_defaults_are_short_for_the_api_and_longer_for_background_processes(env) -> None:
    s = Settings()
    assert s.db_role == "api"
    assert (s.db_statement_timeout_ms, s.db_lock_timeout_ms, s.db_idle_in_tx_timeout_ms) == (
        15_000,
        5_000,
        30_000,
    )
    assert s.db_worker_statement_timeout_ms > s.db_statement_timeout_ms
    assert s.db_worker_lock_timeout_ms >= s.db_lock_timeout_ms
    assert s.db_worker_idle_in_tx_timeout_ms > s.job_deadline_seconds * 1000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_ROLE", "janitor"),
        ("DB_STATEMENT_TIMEOUT_MS", "-1"),
        ("DB_LOCK_TIMEOUT_MS", "-1"),
        ("DB_WORKER_IDLE_IN_TX_TIMEOUT_MS", "-1"),
        ("DB_MIGRATION_LOCK_TIMEOUT_MS", "-1"),
    ],
)
def test_nonsense_timeouts_are_rejected_at_boot(env, name: str, value: str) -> None:
    env.setenv(name, value)
    with pytest.raises(ValueError):  # noqa: PT011
        Settings()


def test_a_worker_idle_limit_that_would_kill_a_dial_in_flight_is_rejected(env) -> None:
    # The worker holds its transaction open while it calls the provider (up to the job deadline).
    env.setenv("JOB_DEADLINE_SECONDS", "25")
    env.setenv("DB_WORKER_IDLE_IN_TX_TIMEOUT_MS", "20000")
    with pytest.raises(ValueError, match="DB_WORKER_IDLE_IN_TX_TIMEOUT_MS"):
        Settings()


def test_zero_disables_a_timeout_and_is_not_checked_against_the_job_deadline(env) -> None:
    env.setenv("DB_WORKER_IDLE_IN_TX_TIMEOUT_MS", "0")
    assert Settings().db_worker_idle_in_tx_timeout_ms == 0


# -- the server really gets the per-role settings ----------------------------------------


@pytest.mark.parametrize("role", ["api", "worker", "analysis", "sweeper", "script"])
def test_each_role_connects_with_its_own_server_side_limits(env, role: str) -> None:
    env.setenv("DB_ROLE", role)
    s = Settings()
    engine = make_engine(s)
    try:
        api = role == "api"
        want = {
            "statement_timeout": (
                s.db_statement_timeout_ms if api else s.db_worker_statement_timeout_ms
            ),
            "lock_timeout": s.db_lock_timeout_ms if api else s.db_worker_lock_timeout_ms,
            "idle_in_transaction_session_timeout": (
                s.db_idle_in_tx_timeout_ms if api else s.db_worker_idle_in_tx_timeout_ms
            ),
        }
        for setting, ms in want.items():
            assert _show(engine, setting) in (f"{ms}ms", f"{ms // 1000}s", f"{ms // 60000}min")
    finally:
        engine.dispose()


def test_the_module_engine_applies_the_limits() -> None:
    from app.core.database import engine

    assert _show(engine, "statement_timeout") != "0"


# -- accept: a statement longer than the limit is cancelled and mapped ------------------------


def _limited_engine(env, **limits: str):
    for name, value in limits.items():
        env.setenv(name, value)
    return make_engine(Settings())


def test_a_statement_over_the_limit_is_cancelled_by_the_server(env) -> None:
    engine = _limited_engine(env, DB_STATEMENT_TIMEOUT_MS="1000")
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError) as caught, engine.connect() as conn:
            conn.execute(text("select pg_sleep(3)"))
        assert type(caught.value.orig).__name__ == "QueryCanceled"
        assert time.monotonic() - started < 2.5
        with engine.connect() as conn:  # the pool is still healthy afterwards
            assert conn.execute(text("select 1")).scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.skipif(_UNDER_FAKETIME, reason="libfaketime distorts asyncio wake-ups")
def test_the_api_answers_503_with_retry_after_for_a_slow_statement(env) -> None:
    from app.core import database

    engine = _limited_engine(env, DB_STATEMENT_TIMEOUT_MS="1000")
    env.setattr(database, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    application = create_app()

    @application.get("/_slow")
    def slow(db: Session = DB_SESSION) -> dict[str, bool]:
        db.execute(text("select pg_sleep(3)"))
        return {"ok": True}

    try:
        response = TestClient(application, raise_server_exceptions=False).get("/_slow")
    finally:
        engine.dispose()
    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert "pg_sleep" not in response.text


# -- accept: a row lock held by another session makes this one fail within lock_timeout ---------


def test_waiting_on_another_sessions_row_lock_fails_within_the_lock_timeout(env) -> None:
    from app.models.campaign import Campaign
    from app.models.enums import CampaignStatus

    name = f"p2-3-lock-{uuid.uuid4().hex[:8]}"
    admin = create_engine(DB_URL, poolclass=NullPool)
    with Session(admin) as s:
        campaign = Campaign(name=name, status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.commit()
        campaign_id = campaign.id
    holder = admin.connect()
    holder.execute(text("select id from campaign where id = :i for update"), {"i": campaign_id})
    engine = _limited_engine(env, DB_LOCK_TIMEOUT_MS="500")
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError) as caught, engine.connect() as conn:
            # Safety net only: without a lock timeout this would wait forever and hang CI.
            conn.execute(text("set local statement_timeout = 10000"))
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


# -- accept: a leaked open transaction is terminated by the idle limit ------------------------


def test_a_leaked_open_transaction_is_terminated_by_the_server(env) -> None:
    engine = _limited_engine(env, DB_IDLE_IN_TX_TIMEOUT_MS="1000")
    admin = create_engine(DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        conn = engine.connect()
        pid = conn.execute(text("select pg_backend_pid()")).scalar_one()  # opens a transaction
        # The code "forgot" to commit or roll back: wait for the server to terminate the session.
        deadline = time.monotonic() + 15
        alive = 1
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            with admin.connect() as a:
                alive = a.execute(
                    text("select count(*) from pg_stat_activity where pid = :p"), {"p": pid}
                ).scalar_one()
        assert alive == 0, "the idle-in-transaction session was not terminated"
        # The kill reaches the client as InternalError (SQLSTATE 25P03), not OperationalError.
        with pytest.raises(InternalError) as caught:
            conn.execute(text("select 1"))
        assert caught.value.orig.sqlstate == "25P03"
        conn.close()
    finally:
        engine.dispose()
        admin.dispose()


# -- every timeout error is a plain OperationalError, so Phase 1's retry paths already apply ------


def test_timeout_errors_are_operational_errors_the_workers_already_retry() -> None:
    import psycopg.errors as pge

    # A statement timeout and a lock timeout. (An idle-in-transaction kill reaches the client as
    # a lost connection; test_a_leaked_open_transaction_is_terminated_by_the_server covers it.)
    for err in (pge.QueryCanceled, pge.LockNotAvailable):
        assert issubclass(err, pge.OperationalError)


# -- alembic: no statement timeout, bounded lock timeout -----------------------------------------


def test_alembic_upgrade_ignores_a_hostile_default_statement_timeout() -> None:
    """A migration role whose default statement_timeout is 1 ms must still migrate."""
    suffix = uuid.uuid4().hex[:8]
    role, database = f"p23_mig_{suffix}", f"p23_mig_{suffix}"
    url = make_url(DB_URL)
    admin = create_engine(
        url.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as c:
        c.execute(text(f"create role {role} login superuser password 'pw'"))
        c.execute(text(f"alter role {role} set statement_timeout = '1ms'"))
        c.execute(text(f"create database {database} owner {role}"))
    try:
        env = dict(os.environ)
        migrator = url.set(username=role, password="pw", database=database)
        env["PRIMARY_DB_URL"] = migrator.render_as_string(hide_password=False)
        for name in _ENV_NAMES:
            env.pop(name, None)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-1500:]
    finally:
        with admin.connect() as c:
            c.execute(text(f"drop database if exists {database} with (force)"))
            c.execute(text(f"drop role if exists {role}"))
        admin.dispose()


# -- API boundary: the idle-in-transaction kill is a 503, other internal faults stay 500 ----------


def _raising_client(exc: Exception) -> TestClient:
    application = create_app()

    @application.get("/_boom")
    def boom() -> None:
        raise exc

    return TestClient(application, raise_server_exceptions=False)


def test_an_idle_in_transaction_kill_is_a_503_with_retry_after() -> None:
    import psycopg.errors as pge

    exc = InternalError("select 1", {}, pge.IdleInTransactionSessionTimeout("terminating"))
    response = _raising_client(exc).get("/_boom")
    assert response.status_code == 503
    assert "Retry-After" in response.headers


def test_other_internal_errors_are_not_disguised_as_unavailability() -> None:
    import psycopg.errors as pge

    exc = InternalError("select 1", {}, pge.DataCorrupted("bad"))
    response = _raising_client(exc).get("/_boom")
    assert response.status_code == 500
