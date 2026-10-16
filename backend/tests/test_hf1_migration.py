"""HF1 1.3 / 1.6 -- the ONE HF1 migration, run for real on a throwaway PostgreSQL database that
ALREADY HOLDS rows: it adds `outbound_control` and the index on
`call_analysis.conversation_session_id` and nothing else; upgrade, downgrade, re-upgrade and
`alembic check` are clean; a downgrade refuses to silently lift an engaged stop.
"""

import logging
import os
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationSession
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus

PRE_HF1 = "c15a1b2c3d4e"
HF1_REV = "e2d1c0b9a8f7"
DB_NAME = "ai_calling_agent_hf1_migration_test"
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


def _seed(engine) -> uuid.UUID:
    """A campaign, contact, attempt, conversation session and an analysis row: the table that
    gets the new index already holds data, with a session reference set."""
    with Session(engine) as s:
        campaign = Campaign(name="pre-hf1", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        contact = Contact(
            campaign_id=campaign.id,
            phone_number="+919800000001",
            normalized_phone_number="+919800000001",
            status=ContactStatus.PENDING,
        )
        s.add(contact)
        s.flush()
        attempt = CallAttempt(
            contact_id=contact.id, attempt_number=1, state=CallAttemptState.INITIATED
        )
        s.add(attempt)
        s.flush()
        session = ConversationSession(call_attempt_id=attempt.id)
        s.add(session)
        s.flush()
        analysis = CallAnalysis(
            call_attempt_id=attempt.id,
            conversation_session_id=session.id,
            contact_id=contact.id,
            campaign_id=campaign.id,
        )
        s.add(analysis)
        s.commit()
        return analysis.id


def _analysis_index_exists(engine) -> bool:
    return any(
        ix["column_names"] == ["conversation_session_id"]
        for ix in inspect(engine).get_indexes("call_analysis")
    )


def test_there_is_exactly_one_head_and_it_is_the_hf1_revision():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [HF1_REV]
    assert script.get_revision(HF1_REV).down_revision == PRE_HF1


def test_upgrade_adds_only_the_table_and_the_index_and_keeps_every_row(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE_HF1)
    analysis_id = _seed(engine)
    tables_before = set(inspect(engine).get_table_names())
    assert "outbound_control" not in tables_before and not _analysis_index_exists(engine)

    command.upgrade(cfg, "head")

    insp = inspect(engine)
    assert set(insp.get_table_names()) - tables_before == {"outbound_control"}
    cols = {c["name"]: c for c in insp.get_columns("outbound_control")}
    assert set(cols) == {
        "key", "engaged", "reason", "category", "engaged_by", "engaged_at",
        "released_by", "released_at",
    }  # fmt: skip
    assert insp.get_pk_constraint("outbound_control")["constrained_columns"] == ["key"]
    assert cols["engaged"]["nullable"] is False
    assert _analysis_index_exists(engine)
    with engine.connect() as c:
        assert c.execute(
            text("SELECT conversation_session_id IS NOT NULL FROM call_analysis WHERE id=:i"),
            {"i": analysis_id},
        ).scalar_one()
        assert c.execute(text("SELECT count(*) FROM outbound_control")).scalar_one() == 0


def test_alembic_check_is_clean_after_upgrade(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if the models and the migrated schema differ


def test_downgrade_then_upgrade_round_trips_on_a_database_with_rows(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, "head")
    analysis_id = _seed(engine)

    command.downgrade(cfg, PRE_HF1)
    assert "outbound_control" not in inspect(engine).get_table_names()
    assert not _analysis_index_exists(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM call_analysis")).scalar_one() == 1

    command.upgrade(cfg, "head")
    assert "outbound_control" in inspect(engine).get_table_names()
    assert _analysis_index_exists(engine)
    with engine.connect() as c:
        assert c.execute(
            text("SELECT id FROM call_analysis WHERE id=:i"), {"i": analysis_id}
        ).scalar_one() == analysis_id
    command.check(cfg)


def test_downgrade_refuses_to_silently_lift_an_engaged_stop(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO outbound_control (key, engaged, reason, engaged_by, engaged_at)"
                " VALUES ('kill_switch', true, 'incident', 'ops', now())"
            )
        )
    with pytest.raises(Exception, match="engaged"):
        command.downgrade(cfg, PRE_HF1)
    assert "outbound_control" in inspect(engine).get_table_names()  # nothing was dropped

    with engine.begin() as c:
        c.execute(text("UPDATE outbound_control SET engaged = false"))
    command.downgrade(cfg, PRE_HF1)  # released: a downgrade is allowed again
    assert "outbound_control" not in inspect(engine).get_table_names()
