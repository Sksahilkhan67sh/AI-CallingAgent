"""Migration upgrade/downgrade/upgrade cycle, run for real against a
throwaway PostgreSQL database (Checkpoint 01A Steps 19, 24, 30) --
not merely inspecting migration source files.
"""

import os

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config

MIGRATION_TEST_DB = "ai_calling_agent_migration_test"
ADMIN_DB_URL = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0] + "/postgres"
MIGRATION_TEST_DB_URL = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0] + f"/{MIGRATION_TEST_DB}"


@pytest.fixture
def migration_test_db():
    admin_conn = psycopg.connect(ADMIN_DB_URL.replace("postgresql+psycopg://", "postgresql://"))
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {MIGRATION_TEST_DB}")
        cur.execute(f"CREATE DATABASE {MIGRATION_TEST_DB}")
    admin_conn.close()

    yield MIGRATION_TEST_DB_URL

    admin_conn = psycopg.connect(ADMIN_DB_URL.replace("postgresql+psycopg://", "postgresql://"))
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {MIGRATION_TEST_DB}")
    admin_conn.close()


def _table_names(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _enum_type_names(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT typname FROM pg_type WHERE typtype = 'e'")
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_cycle_is_clean(migration_test_db, monkeypatch):
    # alembic/env.py deliberately reads the DB URL from app Settings (not
    # a URL an ad-hoc caller passes in) so that real deployments can't
    # accidentally migrate the wrong database -- so exercise that same
    # path here rather than bypassing it.
    from app.core.config import get_settings

    monkeypatch.setenv("PRIMARY_DB_URL", migration_test_db)
    get_settings.cache_clear()

    alembic_cfg = Config("alembic.ini")

    try:
        command.upgrade(alembic_cfg, "head")
        tables_after_first_upgrade = _table_names(migration_test_db)
        assert "contact" in tables_after_first_upgrade
        assert "call_attempt" in tables_after_first_upgrade
        assert "conversation_message" in tables_after_first_upgrade

        command.downgrade(alembic_cfg, "base")
        assert _table_names(migration_test_db) - {"alembic_version"} == set()
        assert _enum_type_names(migration_test_db) == set()

        command.upgrade(alembic_cfg, "head")
        assert _table_names(migration_test_db) == tables_after_first_upgrade
    finally:
        get_settings.cache_clear()
