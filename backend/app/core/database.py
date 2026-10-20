"""
SQLAlchemy engine and session management.

One engine is created per process (not per request) and connection
pooling is handled by SQLAlchemy/psycopg. Each request gets its own
scoped `Session` via the `get_db` FastAPI dependency, which always closes
the session afterward and rolls back on error.

Schema changes go through Alembic migrations (see `backend/alembic/`) --
this module intentionally does not expose a `create_all()` startup path.
"""

from collections.abc import Generator
from typing import Any

from fastapi import Depends
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

settings = get_settings()


def role_timeouts_ms(s: Settings) -> dict[str, int]:
    """Server-side limits for this process's role (P2-3). 0 means no limit, as in PostgreSQL."""
    if s.db_role == "api":
        return {
            "statement_timeout": s.db_statement_timeout_ms,
            "lock_timeout": s.db_lock_timeout_ms,
            "idle_in_transaction_session_timeout": s.db_idle_in_tx_timeout_ms,
        }
    return {
        "statement_timeout": s.db_worker_statement_timeout_ms,
        "lock_timeout": s.db_worker_lock_timeout_ms,
        "idle_in_transaction_session_timeout": s.db_worker_idle_in_tx_timeout_ms,
    }


def migration_timeouts_ms(s: Settings) -> dict[str, int]:
    """Alembic: no statement or idle limit, a bounded lock wait."""
    return {
        "statement_timeout": 0,
        "lock_timeout": s.db_migration_lock_timeout_ms,
        "idle_in_transaction_session_timeout": 0,
    }


def server_options(timeouts_ms: dict[str, int]) -> str:
    """The libpq `options` string that applies the limits to every new connection."""
    return " ".join(f"-c {name}={ms}" for name, ms in timeouts_ms.items())


def make_engine(s: Settings, **kwargs: Any) -> Engine:
    """Build an engine from the pool and timeout settings (P2-2, P2-3). Extra keyword arguments
    go to `create_engine` (used by tests)."""
    connect_args = dict(kwargs.pop("connect_args", {}))
    connect_args["options"] = server_options(role_timeouts_ms(s))
    return create_engine(
        s.primary_db_url,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        pool_timeout=s.db_pool_timeout,
        pool_recycle=s.db_pool_recycle if s.db_pool_recycle > 0 else -1,
        pool_pre_ping=s.db_pool_pre_ping,
        connect_args=connect_args,
        **kwargs,
    )


engine = make_engine(settings)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# HF2: FastAPI 0.118 moved the exit code of a request-scoped `yield` dependency to AFTER the
# response has been sent, which would make the `db.commit()` above run after the client already
# has its 2xx (and turn a failed commit into a silent success). `scope="function"` runs the exit
# code once the endpoint has returned and the response has been built, but BEFORE it is sent.
# Every route and dependency uses this one marker so they all share ONE session per request
# (the dependency cache is keyed by scope); tests/test_hf2_get_db_commit_timing.py enforces it.
DB_SESSION = Depends(get_db, scope="function")


def threadpool_warnings(s: Settings) -> list[str]:
    """Why the API threadpool may outgrow the pool (P2-4). A warning, not an error: when many
    requests never touch the database, more threads than connections is a fair choice."""
    if s.effective_api_threadpool_size <= s.pool_capacity:
        return []
    return [
        f"API_THREADPOOL_SIZE ({s.effective_api_threadpool_size}) exceeds the pool capacity "
        f"({s.pool_capacity} = DB_POOL_SIZE + DB_MAX_OVERFLOW): if that many requests need the "
        f"database at once, the surplus waits up to DB_POOL_TIMEOUT ({s.db_pool_timeout:g} s) "
        "and then gets a 503"
    ]
