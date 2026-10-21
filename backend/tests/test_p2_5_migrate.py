"""P2-5: migrations run as a one-shot job and are safe under concurrency.

Real PostgreSQL: each scenario creates its own throwaway database and runs the real `alembic`
CLI in subprocesses. No test depends on the time of day."""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings

DB_URL = make_url(os.environ["PRIMARY_DB_URL"])
BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
_MIG_ENV = (
    "DB_MIGRATION_LOCK_TIMEOUT_MS",
    "DB_MIGRATION_LOCK_WAIT_SECONDS",
    "DB_MIGRATION_LOCK_RETRIES",
    "DB_MIGRATION_RETRY_DELAY_SECONDS",
)
# The revision before the current head; the head adds conversation_session columns.
PREVIOUS_HEAD = "a4b5c6d7e8f9"


def _admin():
    return create_engine(
        DB_URL.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )


@pytest.fixture
def fresh_db():
    name = f"p25_{uuid.uuid4().hex[:8]}"
    admin = _admin()
    with admin.connect() as c:
        c.execute(text(f"create database {name}"))
    try:
        yield name
    finally:
        with admin.connect() as c:
            c.execute(text(f"drop database if exists {name} with (force)"))
        admin.dispose()


def _env(db: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _MIG_ENV}
    env["PRIMARY_DB_URL"] = DB_URL.set(database=db).render_as_string(hide_password=False)
    env.update(extra)
    return env


def _alembic(db: str, *args: str, **extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=_env(db, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run(db: str, *args: str, **extra: str) -> tuple[int, str]:
    proc = _alembic(db, *args, **extra)
    out, _ = proc.communicate(timeout=240)
    return proc.returncode, out


def _version(db: str) -> list[str]:
    engine = create_engine(DB_URL.set(database=db), poolclass=NullPool)
    try:
        with engine.connect() as c:
            return [r[0] for r in c.execute(text("select version_num from alembic_version"))]
    finally:
        engine.dispose()


def _head() -> str:
    (head,) = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_heads()
    return head


def _hold_table_lock(db: str):
    """Another session holding a conflicting lock on conversation_session (an open transaction)."""
    engine = create_engine(DB_URL.set(database=db), poolclass=NullPool)
    conn = engine.connect()
    conn.execute(text("lock table conversation_session in access exclusive mode"))
    return engine, conn


# -- the schema has exactly one head ---------------------------------------------------------


def test_there_is_a_single_alembic_head() -> None:
    assert len(ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_heads()) == 1


# -- accept: two concurrent upgrades on a fresh database ---------------------------------------


def test_two_concurrent_upgrades_both_succeed_and_reach_one_head(fresh_db: str) -> None:
    first, second = _alembic(fresh_db, "upgrade", "head"), _alembic(fresh_db, "upgrade", "head")
    out1, _ = first.communicate(timeout=240)
    out2, _ = second.communicate(timeout=240)
    assert (first.returncode, second.returncode) == (0, 0), out1[-800:] + out2[-800:]
    for out in (out1, out2):
        assert "already exists" not in out and "UniqueViolation" not in out
    assert _version(fresh_db) == [_head()]


# -- accept: lock_timeout is respected; retry, then abort cleanly --------------------------------


def _blocked_setup(db: str) -> None:
    assert _run(db, "upgrade", "head")[0] == 0
    assert _run(db, "downgrade", "-1")[0] == 0
    assert _version(db) == [PREVIOUS_HEAD]


def test_a_migration_blocked_by_a_lock_aborts_cleanly_after_its_retries(fresh_db: str) -> None:
    _blocked_setup(fresh_db)
    engine, holder = _hold_table_lock(fresh_db)
    try:
        started = time.monotonic()
        code, out = _run(
            fresh_db,
            "upgrade",
            "head",
            DB_MIGRATION_LOCK_TIMEOUT_MS="300",
            DB_MIGRATION_LOCK_RETRIES="2",
            DB_MIGRATION_RETRY_DELAY_SECONDS="1",
        )
        assert code != 0
        assert "blocked by a lock" in out, out[-1200:]
        assert time.monotonic() - started < 60
        assert _version(fresh_db) == [PREVIOUS_HEAD]  # nothing half-applied
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()


def test_a_blocked_migration_succeeds_once_the_blocker_goes_away(fresh_db: str) -> None:
    _blocked_setup(fresh_db)
    engine, holder = _hold_table_lock(fresh_db)
    try:
        proc = _alembic(
            fresh_db,
            "upgrade",
            "head",
            DB_MIGRATION_LOCK_TIMEOUT_MS="300",
            DB_MIGRATION_LOCK_RETRIES="12",
            DB_MIGRATION_RETRY_DELAY_SECONDS="1",
        )
        time.sleep(3)  # the migration is retrying against the held lock
        assert proc.poll() is None
        holder.rollback()
        out, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0, out[-1200:]
        assert _version(fresh_db) == [_head()]
    finally:
        holder.close()
        engine.dispose()


def test_waiting_for_another_migrator_is_bounded(fresh_db: str) -> None:
    from app.core.migration_lock import MIGRATION_LOCK_KEY

    engine = create_engine(DB_URL.set(database=fresh_db), poolclass=NullPool)
    holder = engine.connect()
    holder.execute(text("select pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
    try:
        started = time.monotonic()
        code, out = _run(fresh_db, "upgrade", "head", DB_MIGRATION_LOCK_WAIT_SECONDS="2")
        assert code != 0
        assert "another migration" in out, out[-1200:]
        assert time.monotonic() - started < 60
        assert _version_table_missing(fresh_db)  # it never started migrating
    finally:
        holder.close()
        engine.dispose()


def _version_table_missing(db: str) -> bool:
    engine = create_engine(DB_URL.set(database=db), poolclass=NullPool)
    try:
        with engine.connect() as c:
            return not c.execute(text("select to_regclass('public.alembic_version')")).scalar_one()
    finally:
        engine.dispose()


# -- settings ----------------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for name in _MIG_ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_migration_retry_settings_have_defaults(env) -> None:
    s = Settings()
    assert (s.db_migration_lock_wait_seconds, s.db_migration_lock_retries) == (300, 3)
    assert s.db_migration_retry_delay_seconds == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_MIGRATION_LOCK_WAIT_SECONDS", "0"),
        ("DB_MIGRATION_LOCK_RETRIES", "0"),
        ("DB_MIGRATION_RETRY_DELAY_SECONDS", "-1"),
    ],
)
def test_nonsense_migration_settings_are_rejected(env, name: str, value: str) -> None:
    env.setenv(name, value)
    with pytest.raises(ValueError):  # noqa: PT011
        Settings()


# -- the entrypoint only migrates when asked ------------------------------------------------------


def _entrypoint(tmp_path: Path, run_migrations: str | None, alembic_exit: int = 0):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    script = fake / "alembic"
    script.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit {alembic_exit}\n')
    script.chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin"}
    if run_migrations is not None:
        env["RUN_MIGRATIONS"] = run_migrations
    result = subprocess.run(  # noqa: S603
        ["/bin/sh", str(BACKEND / "docker-entrypoint.sh"), "echo", "started"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text().strip()


@pytest.mark.parametrize("value", [None, "0", ""])
def test_the_entrypoint_does_not_migrate_by_default(tmp_path: Path, value: str | None) -> None:
    result, calls = _entrypoint(tmp_path, value)
    assert (result.returncode, result.stdout.strip(), calls) == (0, "started", "")


def test_the_entrypoint_migrates_when_asked_and_stops_if_that_fails(tmp_path: Path) -> None:
    result, calls = _entrypoint(tmp_path, "1")
    assert (result.returncode, result.stdout.strip(), calls) == (0, "started", "upgrade head")
    result, calls = _entrypoint(tmp_path, "1", alembic_exit=1)
    assert result.returncode != 0 and "started" not in result.stdout


# -- compose wiring [READ] ----------------------------------------------------------


def test_compose_runs_migrations_once_in_their_own_service() -> None:
    services = COMPOSE["services"]
    migrate = services["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate.get("restart") == "no"
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("backend", "worker", "analysis-worker", "sweeper"):
        wait = services[name]["depends_on"]["migrate"]["condition"]
        assert wait == "service_completed_successfully", name
        env = [str(e) for e in services[name].get("environment", [])]
        assert "RUN_MIGRATIONS=1" not in env, f"{name} must not migrate itself"


def test_the_env_example_keeps_migrations_off_by_default() -> None:
    lines = (BACKEND / ".env.example").read_text().splitlines()
    assert "RUN_MIGRATIONS=0" in lines
