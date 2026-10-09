"""Checkpoint 09 follow-up -- identical webhook deliveries racing each
other must collapse to exactly ONE logical processing.

Before the fix, two concurrent deliveries could both pass the
ProcessedEvent existence check; the loser then crashed with an
IntegrityError (HTTP 500) at insert time. These tests use real threads,
independent DB sessions, and the real app/get_db commit path -- the
only way to exercise the actual unique-constraint race.
"""

import os
import random
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.main import app
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationSession
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.processed_event import ProcessedEvent
from app.models.retry_policy import RetryPolicy
from app.services.recovery.factory import get_recovery_scheduler
from tests.phone_helpers import normalize_phone_number

_Session = sessionmaker(bind=create_engine(os.environ["PRIMARY_DB_URL"]))


def _committed_attempt(run_id: int) -> tuple[str, str]:
    phone = f"989-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
    with _Session() as s:
        campaign = Campaign(name="webhook race test", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        s.add(RetryPolicy(campaign_id=campaign.id))  # defaults: max_retries=2, 30s/10min
        contact = Contact(
            campaign_id=campaign.id,
            phone_number=phone,
            normalized_phone_number=normalize_phone_number(phone),
            status=ContactStatus.DIALING,
        )
        s.add(contact)
        s.flush()
        attempt = CallAttempt(
            contact_id=contact.id,
            attempt_number=1,
            provider="dograh",
            provider_call_id=str(run_id),
            state=CallAttemptState.INITIATED,
        )
        s.add(attempt)
        s.commit()
        return str(attempt.id), str(contact.id)


def _fire_concurrently(n: int, body: dict) -> list:
    headers = {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}
    barrier = threading.Barrier(n)
    responses: list = [None] * n

    def deliver(i: int) -> None:
        with TestClient(app) as c:
            barrier.wait()
            responses[i] = c.post(
                "/api/v1/webhooks/dograh/call-completed", json=body, headers=headers
            )

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return responses


def _count(s, model, *conds) -> int:
    return s.execute(select(func.count()).select_from(model).where(*conds)).scalar_one()


@pytest.mark.parametrize("n", [2, 8, 16])
def test_concurrent_duplicate_completion_is_processed_exactly_once(n, redis_client):
    run_id = random.randint(10_000_000, 99_999_999)
    attempt_id, _ = _committed_attempt(run_id)

    responses = _fire_concurrently(
        n,
        {"call_attempt_id": attempt_id, "workflow_run_id": run_id, "call_status": "completed"},
    )

    assert all(r is not None and r.status_code == 200 for r in responses), [
        getattr(r, "status_code", None) for r in responses
    ]
    outcomes = sorted(r.json()["outcome"] for r in responses)
    assert outcomes.count("ended_normally") == 1
    assert outcomes.count("already_processed") == n - 1

    with _Session() as s:
        attempt = s.get(CallAttempt, attempt_id)
        assert attempt.state == CallAttemptState.ENDED_NORMALLY
        assert _count(s, ProcessedEvent, ProcessedEvent.event_id == f"dograh:{run_id}") == 1
        sessions = _count(s, ConversationSession, ConversationSession.call_attempt_id == attempt.id)
        assert sessions == 1
        for action in ("call_attempt.ended_normally_via_dograh_webhook", "analysis.queued"):
            assert (
                _count(s, AuditLog, AuditLog.entity_id == attempt.id, AuditLog.action == action)
                == 1
            ), action


@pytest.mark.parametrize("n", [2, 8, 16])
def test_concurrent_duplicate_never_connected_schedules_recovery_once(n, redis_client):
    run_id = random.randint(10_000_000, 99_999_999)
    attempt_id, contact_id = _committed_attempt(run_id)

    responses = _fire_concurrently(
        n,
        {"call_attempt_id": attempt_id, "workflow_run_id": run_id, "call_status": "no_answer"},
    )

    assert all(r is not None and r.status_code == 200 for r in responses)
    outcomes = sorted(r.json()["outcome"] for r in responses)
    assert outcomes.count("never_connected") == 1
    assert outcomes.count("already_processed") == n - 1

    scheduled = get_recovery_scheduler().due_jobs(datetime.now(UTC) + timedelta(days=1))
    assert sum(1 for j in scheduled if contact_id in j) == 1  # exactly one retry scheduled
    with _Session() as s:
        assert _count(s, ProcessedEvent, ProcessedEvent.event_id == f"dograh:{run_id}") == 1


def test_concurrent_duplicate_telephony_webhook_is_idempotent():
    """The sibling telephony webhook shared the same check-then-insert
    race and now uses the same claim helper."""
    from app.schemas.webhook import TelephonyCallStatusWebhook
    from app.services.webhook_service import WebhookService

    provider_call_id = f"tel-{random.randint(10_000_000, 99_999_999)}"
    event_id = f"evt-{provider_call_id}"
    attempt_id, _ = _committed_attempt(0)
    with _Session() as s:
        s.get(CallAttempt, attempt_id).provider_call_id = provider_call_id
        s.commit()

    n = 8
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def deliver(i: int) -> None:
        payload = TelephonyCallStatusWebhook(
            event_id=event_id, provider_call_id=provider_call_id, status="connected"
        )
        with _Session() as s:
            barrier.wait()
            try:
                results[i] = WebhookService(s).process_call_status(payload)["status"]
                s.commit()
            except Exception as exc:  # the old behaviour: IntegrityError
                results[i] = f"ERR:{type(exc).__name__}"

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(results) == ["already_processed"] * (n - 1) + ["processed"], results
    with _Session() as s:
        assert _count(s, ProcessedEvent, ProcessedEvent.event_id == event_id) == 1
        assert (
            _count(
                s,
                AuditLog,
                AuditLog.entity_id == attempt_id,
                AuditLog.action == "call_attempt.status_updated_via_webhook",
            )
            == 1
        )
