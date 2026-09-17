"""
Shared test fixtures.

Tests run against a real PostgreSQL database (ai_calling_agent_test),
never SQLite -- Postgres-specific behavior (JSONB, native enums, the
`jsonb_array_length` CHECK constraint) would silently not be exercised
otherwise. Each test runs inside its own transaction that is rolled back
at teardown, so tests are isolated without needing to truncate tables.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault(
    "PRIMARY_DB_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/ai_calling_agent_test",
)

from app.core.database import get_db  # noqa: E402
from app.main import app  # noqa: E402

test_engine = create_engine(os.environ["PRIMARY_DB_URL"])
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


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
    yield TestClient(app)
    app.dependency_overrides.clear()
