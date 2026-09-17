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

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(settings.primary_db_url, pool_size=settings.primary_db_pool_size)

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
