"""CP14B migration, run for real against a throwaway PostgreSQL database that ALREADY HOLDS
analysis rows (the lease CHECK constraint and the downgrade guard are only interesting with
data in the table)."""

import logging
import os
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from alembic.config import Config
from app.core.config import get_settings

PRE_CP14B = "c14b4e5f6a7b"
DB_NAME = "ai_calling_agent_cp14b_migration_test"
_BASE = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0]
ADMIN_URL = (_BASE + "/postgres").replace("postgresql+psycopg://", "postgresql://")
DB_URL = f"{_BASE}/{DB_NAME}"


def _admin(sql: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture
def scratch(monkeypatch):
    logger_state = {
        name: lg.disabled
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    _admin(f"DROP DATABASE IF EXISTS {DB_NAME}")
    _admin(f"CREATE DATABASE {DB_NAME}")
    monkeypatch.setenv("PRIMARY_DB_URL", DB_URL)
    get_settings.cache_clear()
    engine = create_engine(DB_URL)
    yield Config("alembic.ini"), engine
    engine.dispose()
    monkeypatch.undo()
    get_settings.cache_clear()
    for name, was_disabled in logger_state.items():
        logging.getLogger(name).disabled = was_disabled
    _admin(f"DROP DATABASE IF EXISTS {DB_NAME}")


def _seed(engine):
    """A campaign/contact/attempt and one analysis row per pre-CP14B status."""
    ids = {"processing": uuid.uuid4(), "completed": uuid.uuid4(), "failed": uuid.uuid4()}
    with engine.begin() as c:
        campaign = uuid.uuid4()
        c.execute(
            text("INSERT INTO campaign (id, name, status) VALUES (:i, 'legacy', 'active')"),
            {"i": campaign},
        )
        for n, (status, aid) in enumerate(ids.items()):
            contact, attempt = uuid.uuid4(), uuid.uuid4()
            c.execute(
                text(
                    "INSERT INTO contact (id, campaign_id, phone_number, normalized_phone_number,"
                    " status, attempt_count) VALUES (:i, :c, :p, :n, 'Completed', 0)"
                ),
                {"i": contact, "c": campaign, "p": f"98000001{n}0", "n": f"+9198000001{n}0"},
            )
            c.execute(
                text(
                    "INSERT INTO call_attempt (id, contact_id, attempt_number, state, started_at)"
                    " VALUES (:i, :c, 1, 'EndedNormally', now())"
                ),
                {"i": attempt, "c": contact},
            )
            c.execute(
                text(
                    "INSERT INTO call_analysis (id, call_attempt_id, contact_id, campaign_id,"
                    " status, attempt_count, key_facts, objections, customer_needs)"
                    " VALUES (:i, :a, :c, :m, :s, 1, '[]', '[]', '[]')"
                ),
                {"i": aid, "a": attempt, "c": contact, "m": campaign, "s": status},
            )
    return ids


def _status_of(engine, aid):
    with engine.connect() as c:
        return c.execute(
            text("SELECT status FROM call_analysis WHERE id = :i"), {"i": aid}
        ).scalar()


def test_upgrade_keeps_every_row_and_recovers_a_leaseless_processing_row(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14B)
    ids = _seed(engine)

    command.upgrade(cfg, "head")

    assert {_status_of(engine, a) for a in ids.values()} == {"processing", "completed", "failed"}
    with engine.connect() as c:
        row = c.execute(
            text(
                "SELECT claim_token, lease_expires_at, lease_expires_at <= now() AS expired,"
                " truncated, last_enqueued_at FROM call_analysis WHERE id = :i"
            ),
            {"i": ids["processing"]},
        ).one()
        untouched = c.execute(
            text("SELECT claim_token FROM call_analysis WHERE id = :i"), {"i": ids["completed"]}
        ).scalar()
    assert row.claim_token is not None and row.expired is True  # recoverable by the sweeper
    assert row.truncated is False and row.last_enqueued_at is None
    assert untouched is None  # other rows are not rewritten
    cols = {c["name"] for c in inspect(engine).get_columns("call_analysis")}
    assert {"claim_token", "lease_expires_at", "next_attempt_at", "reserved_cost"} <= cols
    assert "dograh_workflow_id" in {c["name"] for c in inspect(engine).get_columns("call_attempt")}
    assert "analysis_budget_day" in inspect(engine).get_table_names()


def test_check_constraint_requires_an_owner_and_lease_for_processing(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14B)
    ids = _seed(engine)
    command.upgrade(cfg, "head")
    with pytest.raises(IntegrityError, match="ck_call_analysis_processing_has_lease"):
        with engine.begin() as c:
            c.execute(
                text(
                    "UPDATE call_analysis SET status = 'processing', claim_token = NULL,"
                    " lease_expires_at = NULL WHERE id = :i"
                ),
                {"i": ids["failed"]},
            )


def test_downgrade_refuses_while_rows_would_be_stranded_then_succeeds_without_data_loss(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14B)
    ids = _seed(engine)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(
            text("UPDATE call_analysis SET status = 'retry_wait' WHERE id = :i"),
            {"i": ids["failed"]},
        )

    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(cfg, PRE_CP14B)
    assert _status_of(engine, ids["failed"]) == "retry_wait"  # nothing was deleted or rewritten

    with engine.begin() as c:  # the operator resolves the stranded row deliberately ...
        c.execute(
            text("UPDATE call_analysis SET status = 'failed' WHERE id = :i"), {"i": ids["failed"]}
        )
    command.downgrade(cfg, PRE_CP14B)  # ... and the downgrade now proceeds
    assert {_status_of(engine, a) for a in ids.values()} == {"processing", "completed", "failed"}
    cols = {c["name"] for c in inspect(engine).get_columns("call_analysis")}
    assert (
        "claim_token" not in cols and "analysis_budget_day" not in inspect(engine).get_table_names()
    )

    command.upgrade(cfg, "head")  # and it is re-runnable (enum values already present)
    assert _status_of(engine, ids["completed"]) == "completed"
