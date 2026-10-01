"""Shared helpers for the CP09 failure-injection tests."""

from datetime import time

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.retry_policy import RetryPolicy
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhTriggerResult


def dograh_engine(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()


def setup_contact(db, *, phone="555-970-0001", campaign=None, with_policy=True):
    if campaign is None:
        campaign = Campaign(name="cp09", status=CampaignStatus.ACTIVE)
        db.add(campaign)
        db.flush()
        if with_policy:
            db.add(
                RetryPolicy(
                    campaign_id=campaign.id,
                    window_start=time(0, 0),
                    window_end=time(23, 59, 59),
                )
            )
            db.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.PENDING,
    )
    db.add(contact)
    db.flush()
    return campaign, contact


def make_queue(redis_client) -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, "test:cp09:calls", "test:cp09:workers")


def make_admission(redis_client, **overrides) -> AdmissionController:
    limits = dict(
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )
    limits.update(overrides)
    return AdmissionController(redis_client, **limits)


def make_breaker(redis_client) -> CircuitBreaker:
    return CircuitBreaker(redis_client, "dograh")


def enqueue(queue, campaign, contact, attempt_number=1) -> DialJob:
    job = DialJob.new(
        campaign_id=campaign.id, contact_id=contact.id, attempt_number=attempt_number
    )
    queue.enqueue(job)
    return job


class FakeDograh:
    """Counts trigger requests -- the central 'no duplicate call' assertion."""

    def __init__(self, *, result=None, error=None, on_trigger=None):
        self.result = result or DograhTriggerResult(4242, "WR-4242")
        self.error = error
        self.on_trigger = on_trigger
        self.triggers = 0

    def trigger_call(self, *, phone_number, initial_context):
        self.triggers += 1
        if self.on_trigger:
            self.on_trigger()
        if self.error:
            raise self.error
        return self.result


def install(monkeypatch, fake) -> None:
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: fake)


def attempts_for(db, contact):
    return db.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).all()


__all__ = ["CallAttemptState"]
