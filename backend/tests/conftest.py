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

# CP14 test-environment defaults. The pre-CP14 suite dials at whatever wall-clock time it is
# run, and a campaign with no retry_policy row is now judged by the DEFAULT policy, so the
# production window (09:00-21:00 IST) and daily cap (1000) would make ~1100 unrelated tests
# pass or fail depending on the hour. These make the environment "always open, effectively
# uncapped"; every CP14 test that exercises the window or cap sets its own values explicitly
# (see tests/test_cp14_*.py), so production defaults are still covered.
os.environ.setdefault("HARD_CALLING_WINDOW_START", "00:00:00")
os.environ.setdefault("HARD_CALLING_WINDOW_END", "23:59:59.999999")
os.environ.setdefault("DEFAULT_CALLING_WINDOW_START", "00:00:00")
os.environ.setdefault("DEFAULT_CALLING_WINDOW_END", "23:59:59.999999")
os.environ.setdefault("DAILY_DIAL_CAP", "10000000")

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
    # CP11: the /api/v1 campaign/contact/analysis routes require a token. `client`
    # carries an admin token so the pre-CP11 functional tests keep exercising
    # behaviour; `anon_client` / `operator_client` exist for the security tests.
    yield TestClient(app, headers=_bearer("admin"))
    app.dependency_overrides.clear()


def _bearer(role: str) -> dict[str, str]:
    from app.services.admin.auth import AdminPrincipal, create_access_token

    token, _ = create_access_token(AdminPrincipal(username=f"test-{role}", role=role))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def anon_client(client: TestClient) -> TestClient:
    return TestClient(app)


@pytest.fixture
def operator_client(client: TestClient) -> TestClient:
    return TestClient(app, headers=_bearer("operator"))


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


@pytest.fixture(autouse=True)
def _reset_spend_cap_state():
    """The budget check is cached in-process; never let one test's cached verdict (or its
    audit/log throttle) leak into the next."""
    from app.services import spend_cap

    spend_cap.reset_state()
    yield
    spend_cap.reset_state()
