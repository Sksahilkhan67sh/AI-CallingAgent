"""CP14 migrations and operational scripts, run for real against throwaway PostgreSQL
databases that ALREADY HOLD DATA (the suppression primary-key change is only interesting
with rows in it)."""

import logging
import os
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from app.core.config import get_settings

PRE_CP14 = "d15c334bf7e7"
CP14_A = "c14a0a1b2c3d"
DB_NAME = "ai_calling_agent_cp14_migration_test"
_BASE = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0]
ADMIN_URL = (_BASE + "/postgres").replace("postgresql+psycopg://", "postgresql://")
DB_URL = f"{_BASE}/{DB_NAME}"


def _admin(sql: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture
def scratch(monkeypatch):
    # alembic/env.py runs logging.config.fileConfig(disable_existing_loggers=True): run
    # in-process it silences every logger that already exists (in production Alembic is a
    # separate process, so app logging is unaffected). Put them back so later tests'
    # caplog assertions still see their logs.
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
    cfg = Config("alembic.ini")
    yield cfg, engine
    engine.dispose()
    monkeypatch.undo()
    get_settings.cache_clear()
    for name, was_disabled in logger_state.items():
        logging.getLogger(name).disabled = was_disabled
    _admin(f"DROP DATABASE IF EXISTS {DB_NAME}")


def _seed_pre_cp14(engine, *, duplicate_number: bool = False):
    """Campaign, three contacts, and old-schema suppression rows (PK contact_id)."""
    ids = {"campaign": uuid.uuid4(), "contacts": [uuid.uuid4() for _ in range(3)]}
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO campaign (id, name, status) VALUES (:i, 'legacy', 'active')"),
            {"i": ids["campaign"]},
        )
        for n, cid in enumerate(ids["contacts"]):
            c.execute(
                text(
                    "INSERT INTO contact (id, campaign_id, phone_number, normalized_phone_number,"
                    " status, attempt_count) VALUES (:i, :c, :p, :n, 'Pending', 0)"
                ),
                {"i": cid, "c": ids["campaign"], "p": f"98000000{n}0", "n": f"+9198000000{n}0"},
            )
        numbers = [
            "+919800000010",
            "+919800000020",
            "+919800000020" if duplicate_number else "+919800000030",
        ]
        for cid, number, when in zip(ids["contacts"], numbers, ("10", "11", "12"), strict=True):
            c.execute(
                text(
                    "INSERT INTO suppression"
                    " (contact_id, phone_number, reason, source, requested_at)"
                    f" VALUES (:i, :p, 'opt-out', 'agent_in_call', '2026-01-01 {when}:00:00+00')"
                ),
                {"i": cid, "p": number},
            )
    return ids


def _cols(engine, table):
    return {c["name"]: c for c in inspect(engine).get_columns(table)}


def test_upgrade_on_a_populated_database_keeps_every_row_and_adds_the_new_capability(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14)
    ids = _seed_pre_cp14(engine)

    command.upgrade(cfg, "head")

    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, contact_id, phone_number, reason, source, created_by"
                " FROM suppression ORDER BY phone_number"
            )
        ).all()
        campaign = c.execute(text("SELECT timezone, default_region FROM campaign")).one()
    assert [r.phone_number for r in rows] == ["+919800000010", "+919800000020", "+919800000030"]
    assert [r.contact_id for r in rows] == ids["contacts"]  # links preserved
    assert all(r.id is not None for r in rows) and len({r.id for r in rows}) == 3
    assert all(r.reason == "opt-out" and r.source == "agent_in_call" for r in rows)
    assert all(r.created_by is None for r in rows)
    assert tuple(campaign) == ("Asia/Kolkata", "IN")  # existing campaigns get the defaults

    assert _cols(engine, "suppression")["contact_id"]["nullable"] is True
    assert "duration_seconds" in _cols(engine, "call_attempt")
    with engine.begin() as c:  # the new capability: a number with no contact
        c.execute(
            text(
                "INSERT INTO suppression (id, phone_number, source)"
                " VALUES (gen_random_uuid(), '+919800000040', 'manual_api')"
            )
        )
    with pytest.raises(Exception, match="uq_suppression_phone_number"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppression (id, phone_number, source)"
                " VALUES (gen_random_uuid(), '+919800000040', 'manual_api')"
            )
        )
    with pytest.raises(Exception, match="uq_suppression_contact_id"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppression (id, contact_id, phone_number, source)"
                " VALUES (gen_random_uuid(), :i, '+919800000099', 'manual_api')"
            ),
            {"i": ids["contacts"][0]},
        )
    command.check(cfg)  # models and migrations agree: no drift


def test_upgrade_refuses_when_numbers_are_duplicated_and_changes_nothing(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14)
    _seed_pre_cp14(engine, duplicate_number=True)

    with pytest.raises(RuntimeError, match="dedupe_suppressions"):
        command.upgrade(cfg, "head")

    # Nothing was applied, nothing was dropped.
    assert "id" not in _cols(engine, "suppression")
    assert "timezone" not in _cols(engine, "campaign")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM suppression")).scalar_one() == 3
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == PRE_CP14


def test_dedupe_script_dry_run_then_apply_then_the_migration_succeeds(scratch):
    from app.scripts import dedupe_suppressions as script

    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14)
    ids = _seed_pre_cp14(engine, duplicate_number=True)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        dry = script.run(db, apply=False)
    assert dry["numbers_with_duplicates"] == 1 and dry["rows_to_delete"] == 1
    assert all("+919800000020" not in line for line in dry["detail"])  # masked
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM suppression")).scalar_one() == 3  # untouched

    with Session() as db:
        applied = script.run(db, apply=True)
    assert applied["rows_deleted"] == 1
    with engine.connect() as c:
        survivors = c.execute(text("SELECT contact_id FROM suppression")).scalars().all()
    # the EARLIEST row for the duplicated number survives (contact 1 at 11:00, not 2 at 12:00)
    assert ids["contacts"][1] in survivors and ids["contacts"][2] not in survivors
    assert len(survivors) == 2

    with Session() as db:  # idempotent
        assert script.run(db, apply=True)["rows_deleted"] == 0
    command.upgrade(cfg, "head")  # and now the constraint can be added


def test_downgrade_refuses_to_drop_contactless_rows_and_otherwise_round_trips(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP14)
    ids = _seed_pre_cp14(engine)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppression (id, phone_number, source)"
                " VALUES (:i, '+919800000050', 'manual_api')"
            ),
            {"i": uuid.uuid4()},
        )

    with pytest.raises(RuntimeError, match="no contact"):
        command.downgrade(cfg, PRE_CP14)
    with engine.connect() as c:  # refused, and nothing was lost
        assert c.execute(text("SELECT count(*) FROM suppression")).scalar_one() == 4
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() != PRE_CP14

    with engine.begin() as c:
        c.execute(text("DELETE FROM suppression WHERE contact_id IS NULL"))
    command.downgrade(cfg, PRE_CP14)
    pk = inspect(engine).get_pk_constraint("suppression")["constrained_columns"]
    assert pk == ["contact_id"] and "id" not in _cols(engine, "suppression")
    with engine.connect() as c:
        assert set(c.execute(text("SELECT contact_id FROM suppression")).scalars()) == set(
            ids["contacts"]
        )
    command.upgrade(cfg, "head")  # and forward again
    command.check(cfg)


def test_a_single_head_and_the_step_between_is_reversible(scratch):
    cfg, engine = scratch
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(cfg).get_heads() == ["c14b4e5f6a7b"]
    command.upgrade(cfg, "head")
    command.downgrade(cfg, PRE_CP14)
    assert "timezone" not in _cols(engine, "campaign")
    assert CP14_A not in {
        r
        for r in engine.connect().execute(text("SELECT version_num FROM alembic_version")).scalars()
    }
    command.upgrade(cfg, "head")


def test_zoneinfo_for_the_default_timezones_loads_here():
    """Loads in THIS environment. Whether it loads in the python:3.11-slim production image
    is BLOCKED/UNVERIFIED (no registry access) -- the settings validator makes a miss a boot
    error rather than a silent UTC fallback."""
    from zoneinfo import ZoneInfo

    for name in (get_settings().default_timezone, get_settings().budget_timezone):
        assert ZoneInfo(name).key == name
