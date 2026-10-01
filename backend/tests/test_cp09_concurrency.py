"""CP09 -- real concurrency: separate sessions/threads against PostgreSQL
and Redis. Data is committed (so other connections can see it) and removed
in the finally block."""

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent, ConversationSession
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.processed_event import ProcessedEvent
from app.models.retry_policy import RetryPolicy
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.phone import normalize_phone_number
from app.services.queue.dialer_worker import process_one_job
from app.services.telephony.dograh_webhook_service import process_dograh_webhook

from . import _cp09 as h


def _cleanup(campaign_id):
    with SessionLocal() as db:
        contact_ids = [c.id for c in db.query(Contact).filter_by(campaign_id=campaign_id)]
        attempt_ids = [
            a.id for a in db.query(CallAttempt).filter(CallAttempt.contact_id.in_(contact_ids))
        ]
        for model, col in (
            (CallAnalysis, CallAnalysis.call_attempt_id),
            (CallEvent, CallEvent.call_attempt_id),
            (ConversationSession, ConversationSession.call_attempt_id),
        ):
            db.execute(delete(model).where(col.in_(attempt_ids)))
        db.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id.like("dograh:%")))
        db.execute(delete(CallAttempt).where(CallAttempt.id.in_(attempt_ids)))
        db.execute(delete(Contact).where(Contact.id.in_(contact_ids)))
        db.execute(delete(RetryPolicy).where(RetryPolicy.campaign_id == campaign_id))
        db.execute(delete(Campaign).where(Campaign.id == campaign_id))
        db.commit()


def test_duplicate_webhook_burst_is_processed_exactly_once(redis_client):
    with SessionLocal() as db:
        campaign = Campaign(name="burst", status=CampaignStatus.ACTIVE)
        db.add(campaign)
        db.flush()
        db.add(RetryPolicy(campaign_id=campaign.id))
        contact = Contact(
            campaign_id=campaign.id,
            phone_number="555-995-0001",
            normalized_phone_number=normalize_phone_number("555-995-0001"),
            status=ContactStatus.DIALING,
            attempt_count=1,
        )
        db.add(contact)
        db.flush()
        attempt = CallAttempt(
            contact_id=contact.id,
            attempt_number=1,
            provider="dograh",
            provider_call_id="31337",
            state=CallAttemptState.INITIATED,
        )
        db.add(attempt)
        db.commit()
        attempt_id, campaign_id = attempt.id, campaign.id

    outcomes: list[str] = []
    gate = threading.Barrier(16)

    def deliver():
        payload = DograhWebhookPayload(
            call_attempt_id=attempt_id, call_status="user_hangup", duration_seconds=70
        )
        with SessionLocal() as db:
            gate.wait()
            try:
                result = process_dograh_webhook(db, payload)
                db.commit()
                outcomes.append(result.outcome)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                outcomes.append(f"error:{type(exc).__name__}")

    try:
        with ThreadPoolExecutor(16) as pool:
            for f in [pool.submit(deliver) for _ in range(16)]:
                f.result()

        assert outcomes.count("ended_normally") == 1, outcomes
        assert outcomes.count("already_processed") == 15, outcomes
        with SessionLocal() as db:
            assert db.query(ProcessedEvent).filter_by(event_id=f"dograh:{attempt_id}").count() == 1
            transitions = db.query(CallEvent).filter_by(
                call_attempt_id=attempt_id, event_type="CALL_STATE_TRANSITION"
            )
            assert transitions.count() == 2
    finally:
        _cleanup(campaign_id)


def test_competing_workers_on_a_duplicated_job_place_one_call(redis_client, provider, monkeypatch):
    h.dograh_engine(monkeypatch)
    with SessionLocal() as db:
        campaign, contact = h.setup_contact(db, phone="555-995-0002")
        db.commit()
        campaign_id, contact_id = campaign.id, contact.id
        queue = h.make_queue(redis_client)
        for _ in range(8):  # at-least-once delivery duplicated the same job
            h.enqueue(queue, campaign, contact)

    fake = h.FakeDograh()
    h.install(monkeypatch, fake)
    gate = threading.Barrier(8)
    outcomes: list[str] = []

    def worker(i):
        with SessionLocal() as db:
            gate.wait()
            try:
                outcomes.append(
                    process_one_job(
                        db,
                        queue,
                        h.make_admission(redis_client),
                        provider,
                        h.make_breaker(redis_client),
                        consumer_name=f"w{i}",
                    )
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                outcomes.append(f"error:{type(exc).__name__}")

    try:
        with ThreadPoolExecutor(8) as pool:
            for f in [pool.submit(worker, i) for i in range(8)]:
                f.result()
        assert fake.triggers == 1, (fake.triggers, outcomes)
        with SessionLocal() as db:
            assert db.query(CallAttempt).filter_by(contact_id=contact_id).count() == 1
    finally:
        _cleanup(campaign_id)
        get_settings.cache_clear()
