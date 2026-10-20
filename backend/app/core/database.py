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


def make_engine(s: Settings, **kwargs: Any) -> Engine:
    """Build an engine from the pool settings (P2-2). Extra keyword arguments go to
    `create_engine` (used by tests and by role-specific options)."""
    return create_engine(
        s.primary_db_url,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        pool_timeout=s.db_pool_timeout,
        pool_recycle=s.db_pool_recycle if s.db_pool_recycle > 0 else -1,
        pool_pre_ping=s.db_pool_pre_ping,
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
