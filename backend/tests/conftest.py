"""
Shared test fixtures.

Tests run against a real PostgreSQL database (ai_calling_agent_test),
never SQLite -- Postgres-specific behavior (JSONB, native enums, the
`jsonb_array_length` CHECK constraint) would silently not be exercised
otherwise. Each test runs inside its own transaction that is rolled back
at teardown, so tests are isolated without needing to truncate tables.

Checkpoint 03 adds a real Redis fixture (database index 1, distinct
from the dev default of 0) for queue/admission/circuit-breaker tests --
never a fake in-memory substitute, per Step 31.
"""

import os

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault(
    "PRIMARY_DB_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/ai_calling_agent_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

from app.core.database import get_db  # noqa: E402
from app.main import app  # noqa: E402

test_engine = create_engine(os.environ["PRIMARY_DB_URL"])
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def _clean_test_database():
    """Several tests (real-thread concurrency tests) must COMMIT real rows,
    which the per-test rollback cannot undo. Without this, those rows leak
    into later runs and break count-based tests -- on unmodified main a
    second consecutive run against the same DB failed 5 tests. Truncating
    once per session makes the suite repeatable. Test database only."""
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with test_engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def db_session() -> Session:
    connection = test_engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(bind=connection)

    yield session

    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session: Session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # Checkpoint 09: every test gets a clean Redis regardless of
    # whether it also requests the redis_client fixture -- without
    # this, Redis-backed state (rate limiting, circuit breakers, queue
    # streams written incidentally through the app) leaks across tests
    # that only use `client`, not `redis_client` directly.
    redis_lib.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True).flushdb()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def redis_client() -> redis_lib.Redis:
    client = redis_lib.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
def provider():
    from app.services.telephony.mock_provider import MockTelephonyProvider

    return MockTelephonyProvider()
