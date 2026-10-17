"""P1-2 migration, run for real on a throwaway PostgreSQL database that ALREADY holds rows:
it adds three nullable columns and one partial index on `contact`, backfills the contacts that are
RETRY_SCHEDULED today (their schedule lives only in Redis), and is reversible.
"""

import logging
import os
import uuid

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus

PRE = "e2d1c0b9a8f7"
REV = "f3a4b5c6d7e8"
DB_NAME = "ai_calling_agent_p12_migration_test"
_BASE = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0]
ADMIN_URL = (_BASE + "/postgres").replace("postgresql+psycopg://", "postgresql://")
DB_URL = f"{_BASE}/{DB_NAME}"
NEW_COLUMNS = {"next_retry_at", "retry_attempt_id", "retry_attempt_number"}


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


def _columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("contact")}


def _seed_pre_migration(engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A RETRY_SCHEDULED contact with two attempts and a PENDING contact. Contacts are inserted
    with Core and explicit columns: the ORM model already knows the new columns, the schema
    at this revision does not."""
    with Session(engine) as s:
        campaign = Campaign(name="pre-p1-2", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        retry_id, pending_id = uuid.uuid4(), uuid.uuid4()
        for contact_id, phone, status, count in (
            (retry_id, "+919800000011", ContactStatus.RETRY_SCHEDULED, 2),
            (pending_id, "+919800000012", ContactStatus.PENDING, 0),
        ):
            s.execute(
                Contact.__table__.insert().values(
                    id=contact_id,
                    campaign_id=campaign.id,
                    phone_number=phone,
                    normalized_phone_number=phone,
                    status=status,
                    attempt_count=count,
                )
            )
        s.flush()
        last_attempt = uuid.uuid4()
        for number, attempt_id in ((1, uuid.uuid4()), (2, last_attempt)):
            s.add(
                CallAttempt(
                    id=attempt_id,
                    contact_id=retry_id,
                    attempt_number=number,
                    state=CallAttemptState.FAILED_TO_CONNECT,
                )
            )
        s.commit()
    return retry_id, pending_id, last_attempt


def test_there_is_exactly_one_head():
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1


def test_upgrade_backfills_the_retry_scheduled_contact_and_touches_nothing_else(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE)
    assert not NEW_COLUMNS & _columns(engine)
    retry_id, pending_id, last_attempt = _seed_pre_migration(engine)

    command.upgrade(cfg, REV)

    assert NEW_COLUMNS <= _columns(engine)
    indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("contact")}
    index = indexes["ix_contact_next_retry_at"]
    assert index["column_names"] == ["next_retry_at"]
    with engine.connect() as conn:
        retry = conn.execute(
            text(
                "SELECT next_retry_at, retry_attempt_number, retry_attempt_id "
                "FROM contact WHERE id = :id"
            ),
            {"id": retry_id},
        ).one()
        pending = conn.execute(
            text(
                "SELECT next_retry_at, retry_attempt_number, retry_attempt_id "
                "FROM contact WHERE id = :id"
            ),
            {"id": pending_id},
        ).one()
        total = conn.execute(text("SELECT count(*) FROM contact")).scalar_one()
    assert retry.next_retry_at is not None
    assert retry.retry_attempt_number == 3  # attempt_count (2) + 1
    assert retry.retry_attempt_id == last_attempt  # the most recent attempt
    assert tuple(pending) == (None, None, None)
    assert total == 2  # every row survived


def test_the_migration_is_reversible_and_alembic_check_is_clean(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    command.check(cfg)  # raises if the models and the migrated schema differ

    command.downgrade(cfg, PRE)
    assert not NEW_COLUMNS & _columns(engine)
    assert "ix_contact_next_retry_at" not in {
        ix["name"] for ix in inspect(engine).get_indexes("contact")
    }

    command.upgrade(cfg, "head")
    command.check(cfg)
