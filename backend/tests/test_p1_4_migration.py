"""P1-4 migration on a throwaway database: adds the NeedsReview contact status, refuses a downgrade
while a contact still carries it, and leaves `alembic check` clean."""

import logging
import os
from pathlib import Path

import psycopg
import pytest
import yaml
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus

PRE = "f3a4b5c6d7e8"
REV = "a4b5c6d7e8f9"
DB_NAME = "ai_calling_agent_p14_migration_test"
_BASE = os.environ["PRIMARY_DB_URL"].rsplit("/", 1)[0]
ADMIN_URL = (_BASE + "/postgres").replace("postgresql+psycopg://", "postgresql://")
DB_URL = f"{_BASE}/{DB_NAME}"
COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


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


def _labels(engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'contact_status'"
                )
            ).scalars()
        )


def test_upgrade_adds_the_status_and_alembic_check_is_clean(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, PRE)
    assert "NeedsReview" not in _labels(engine)

    command.upgrade(cfg, REV)
    assert "NeedsReview" in _labels(engine)

    command.upgrade(cfg, "head")  # `check` needs a database at the current head
    command.check(cfg)


def test_downgrade_refuses_while_a_contact_is_needs_review_then_succeeds(scratch):
    cfg, engine = scratch
    command.upgrade(cfg, "head")
    with Session(engine) as s:
        campaign = Campaign(name="p1-4", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        contact = Contact(
            campaign_id=campaign.id,
            phone_number="+919800000021",
            normalized_phone_number="+919800000021",
            status=ContactStatus.NEEDS_REVIEW,
        )
        s.add(contact)
        s.commit()
        contact_id = contact.id

    with pytest.raises(RuntimeError, match="NeedsReview"):
        command.downgrade(cfg, PRE)

    with Session(engine) as s:
        s.get(Contact, contact_id).status = ContactStatus.CLOSED
        s.commit()
    command.downgrade(cfg, PRE)  # allowed: the unused enum value simply stays
    command.upgrade(cfg, "head")  # and re-upgrading is a no-op for it
    command.check(cfg)


def test_compose_runs_a_sweeper_service_with_the_redis_password():
    compose = yaml.safe_load(COMPOSE.read_text())
    sweeper = compose["services"]["sweeper"]
    assert sweeper["command"] == ["python", "-m", "app.sweeper"]
    env = [str(e) for e in sweeper["environment"]]
    assert any(e.startswith("REDIS_URL=") and ":${REDIS_PASSWORD" in e for e in env)
