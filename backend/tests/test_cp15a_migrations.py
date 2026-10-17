"""CP15A migration, run for real against a throwaway PostgreSQL database that ALREADY HOLDS
rows: upgrade keeps everything and backfills only what it can prove; `alembic check` is
clean; the downgrade refuses to destroy erasure evidence or silently lift a legal hold."""

import json
import logging
import os
import uuid

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings

PRE_CP15A = "a83d13f0c415"
DB_NAME = "ai_calling_agent_cp15a_migration_test"
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


def _seed(engine) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("closed_audited", "closed_unaudited", "active")}
    with engine.begin() as c:
        for key, status in (
            ("closed_audited", "completed"),
            ("closed_unaudited", "completed"),
            ("active", "active"),
        ):
            c.execute(
                text("INSERT INTO campaign (id, name, status) VALUES (:i, :n, :s)"),
                {"i": ids[key], "n": key, "s": status},
            )
            c.execute(
                text(
                    "INSERT INTO contact (id, campaign_id, phone_number, normalized_phone_number,"
                    " status, attempt_count) VALUES (gen_random_uuid(), :c, :p, :p, 'Pending', 0)"
                ),
                {"c": ids[key], "p": f"+9198{abs(hash(key)) % 10**8:08d}"},
            )
        c.execute(
            text(
                "INSERT INTO audit_log (id, actor, action, entity_type, entity_id, metadata,"
                " created_at) VALUES (gen_random_uuid(), 'a', 'campaign.status_changed',"
                " 'campaign', :i, CAST(:m AS jsonb), '2026-03-01T00:00:00Z')"
            ),
            {"i": ids["closed_audited"], "m": json.dumps({"from": "active", "to": "completed"})},
        )
    return ids


def test_there_is_exactly_one_alembic_head():
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1


def test_upgrade_keeps_rows_backfills_only_provable_closure_and_check_is_clean(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP15A)
    ids = _seed(engine)

    command.upgrade(cfg, "head")
    command.check(cfg)  # no model/migration drift (raises if there is)

    with engine.connect() as c:
        rows = {
            r.name: r
            for r in c.execute(text("SELECT name, legal_hold, closed_at, status FROM campaign"))
        }
        assert c.execute(text("SELECT count(*) FROM contact")).scalar() == 3
        assert c.execute(text("SELECT bool_or(legal_hold) FROM contact")).scalar() is False
        assert c.execute(text("SELECT count(purged_at) FROM contact")).scalar() == 0
    assert str(rows["closed_audited"].closed_at).startswith("2026-03-01")
    assert rows["closed_unaudited"].closed_at is None  # unknown => never auto-purged
    assert rows["active"].closed_at is None
    assert not any(r.legal_hold for r in rows.values())
    assert ids  # seeded
    inspector = inspect(engine)
    assert {"erasure_log", "retention_run"} <= set(inspector.get_table_names())
    index_names = {
        i["name"] for t in ("conversation_message", "processed_event", "working_memory_snapshot")
        for i in inspector.get_indexes(t)
    }
    assert {
        "ix_conversation_message_created_at",
        "ix_processed_event_received_at",
        "ix_working_memory_snapshot_snapshotted_at",
    } <= index_names


def test_downgrade_refuses_to_destroy_erasure_evidence_or_lift_a_hold(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_CP15A)
    ids = _seed(engine)
    command.upgrade(cfg, "head")

    with engine.begin() as c:
        c.execute(
            text("INSERT INTO erasure_log (id, fingerprint, actor, counts)"
                 " VALUES (gen_random_uuid(), :f, 'a', '{}')"),
            {"f": "0" * 64},
        )
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(cfg, PRE_CP15A)

    # the log is append-only: deleting it takes a deliberate, explicit step
    with engine.begin() as c:
        c.execute(text("ALTER TABLE erasure_log DISABLE TRIGGER erasure_log_append_only"))
        c.execute(text("DELETE FROM erasure_log"))
        c.execute(
            text("UPDATE campaign SET legal_hold = true WHERE id = :i"), {"i": ids["active"]}
        )
    with pytest.raises(RuntimeError, match="legal hold"):
        command.downgrade(cfg, PRE_CP15A)
    with engine.connect() as c:  # nothing was changed by either refusal
        assert c.execute(text("SELECT legal_hold FROM campaign WHERE name='active'")).scalar()

    with engine.begin() as c:
        c.execute(text("UPDATE campaign SET legal_hold = false"))
    command.downgrade(cfg, PRE_CP15A)
    inspector = inspect(engine)
    assert "erasure_log" not in inspector.get_table_names()
    assert "legal_hold" not in {c["name"] for c in inspector.get_columns("campaign")}
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM contact")).scalar() == 3  # data intact
    command.upgrade(cfg, "head")  # re-runnable
    command.check(cfg)
