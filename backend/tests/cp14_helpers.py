"""Shared helpers for the CP14 integration tests that need COMMITTED data.

`dispatch_due_recovery_jobs`, the budget gate's audit write and the worker threads open their
own sessions, so these tests cannot live inside the rolled-back `db_session` transaction.
Numbers come from a process-wide counter (suppression is global, so a number must never be
reused by another test); the module-scoped fixture truncates everything afterwards."""

import itertools
import os
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.base import Base
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.models.retry_policy import RetryPolicy
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.redis_queue import RedisStreamQueue
from tests.phone_helpers import valid_in

engine = create_engine(os.environ["PRIMARY_DB_URL"])
Session = sessionmaker(bind=engine)

IST = ZoneInfo("Asia/Kolkata")
_numbers = itertools.count(60_000_000 + (os.getpid() % 1000) * 10_000)


def next_phone() -> str:
    return valid_in(next(_numbers))


def ist(hour: int, minute: int = 0, day: int = 6) -> datetime:
    """A wall-clock instant in Asia/Kolkata on 2026-01-<day>, as aware UTC."""
    return datetime(2026, 1, day, hour, minute, tzinfo=IST).astimezone(UTC)


class Clock:
    """The injected clock: `clock()` is what the dialer/budget treat as 'now'."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture(scope="module", autouse=True)
def truncate_after_module():
    yield
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture
def production_window(monkeypatch):
    """The real production bounds (09:00-21:00), which the suite-wide test env widens."""
    s = get_settings()
    monkeypatch.setattr(s, "hard_calling_window_start", time(9, 0))
    monkeypatch.setattr(s, "hard_calling_window_end", time(21, 0))
    monkeypatch.setattr(s, "default_calling_window_start", time(9, 0))
    monkeypatch.setattr(s, "default_calling_window_end", time(21, 0))
    return s


def commit_world(
    *,
    contacts: int = 1,
    policy: dict | None = None,
    status: CampaignStatus = CampaignStatus.ACTIVE,
    timezone: str = "Asia/Kolkata",
):
    """Committed campaign (+ optional policy row) with `contacts` PENDING contacts.
    policy=None leaves the campaign LEGACY (no retry_policy row)."""
    with Session() as s:
        campaign = Campaign(name="cp14", status=status, timezone=timezone, default_region="IN")
        s.add(campaign)
        s.flush()
        if policy is not None:
            s.add(RetryPolicy(campaign_id=campaign.id, **policy))
        ids = []
        for _ in range(contacts):
            phone = next_phone()
            contact = Contact(
                campaign_id=campaign.id,
                phone_number=phone,
                normalized_phone_number=phone,
                status=ContactStatus.PENDING,
            )
            s.add(contact)
            s.flush()
            ids.append(contact.id)
        s.commit()
        return campaign.id, ids


def make_queue(redis_client, name: str = "cp14") -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, f"test:{name}:calls", f"test:{name}:workers")


def make_admission(redis_client) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=10_000,
        campaign_cps_limit=10_000,
        provider_cps_limit=10_000,
        global_concurrency_limit=10_000,
        campaign_concurrency_limit=10_000,
        provider_concurrency_limit=10_000,
    )
