"""Recovery dispatch -- Checkpoint 05 §9, §11, real Postgres + Redis."""

import os
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus, SuppressionSource
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.services.phone import normalize_phone_number
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import RecoveryScheduler

_engine = create_engine(os.environ["PRIMARY_DB_URL"])
_Session = sessionmaker(bind=_engine)


def _setup(phone, *, campaign_status=CampaignStatus.ACTIVE, with_policy=True, window=None):
    session = _Session()
    campaign = Campaign(name="Dispatch test", status=campaign_status)
    session.add(campaign)
    session.flush()
    if with_policy:
        kwargs = {}
        if window:
            kwargs["window_start"], kwargs["window_end"] = window
        session.add(RetryPolicy(campaign_id=campaign.id, **kwargs))
        session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.RETRY_SCHEDULED,
    )
    session.add(contact)
    session.flush()
    previous_attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.DROPPED_MID_CALL
    )
    session.add(previous_attempt)
    session.flush()
    session.commit()
    ids = (campaign.id, contact.id, previous_attempt.id)
    session.close()
    return ids


def _cleanup(campaign_id, contact_id, *_extra):
    session = _Session()
    session.query(Suppression).filter_by(contact_id=contact_id).delete()
    session.query(AuditLog).filter_by(entity_id=contact_id).delete()
    attempt_ids = [r[0] for r in session.query(CallAttempt.id).filter_by(contact_id=contact_id)]
    if attempt_ids:
        session.query(CallEvent).filter(CallEvent.call_attempt_id.in_(attempt_ids)).delete(
            synchronize_session=False
        )
        session.query(AuditLog).filter(AuditLog.entity_id.in_(attempt_ids)).delete(
            synchronize_session=False
        )
    session.query(CallAttempt).filter_by(contact_id=contact_id).delete()
    session.query(Contact).filter_by(id=contact_id).delete()
    session.query(RetryPolicy).filter_by(campaign_id=campaign_id).delete()
    session.query(Campaign).filter_by(id=campaign_id).delete()
    session.commit()
    session.close()


def test_due_job_is_enqueued_to_the_dialer(redis_client):
    campaign_id, contact_id, attempt_id = _setup("555-991-0001", with_policy=False)
    try:
        scheduler = RecoveryScheduler(redis_client)
        queue = RedisStreamQueue(redis_client, "test:dispatch:calls", "test:dispatch:workers")
        job = RecoveryJob.new(
            attempt_id=attempt_id, contact_id=contact_id, campaign_id=campaign_id,
            attempt_number=2,
        )
        scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))

        dispatched = dispatch_due_recovery_jobs(scheduler, queue)

        assert dispatched == 1
        assert queue.redis.xlen("test:dispatch:calls") == 1
    finally:
        _cleanup(campaign_id, contact_id)


def test_suppressed_contact_is_skipped_not_dispatched(redis_client):
    campaign_id, contact_id, attempt_id = _setup("555-991-0002", with_policy=False)
    session = _Session()
    contact = session.get(Contact, contact_id)
    session.add(
        Suppression(
            contact_id=contact_id, phone_number=contact.normalized_phone_number,
            reason="opt-out", source=SuppressionSource.MANUAL_API,
        )
    )
    session.commit()
    session.close()

    try:
        scheduler = RecoveryScheduler(redis_client)
        queue = RedisStreamQueue(redis_client, "test:dispatch:calls2", "test:dispatch:workers2")
        job = RecoveryJob.new(
            attempt_id=attempt_id, contact_id=contact_id, campaign_id=campaign_id,
            attempt_number=2,
        )
        scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))

        dispatched = dispatch_due_recovery_jobs(scheduler, queue)

        assert dispatched == 0
        assert queue.redis.xlen("test:dispatch:calls2") == 0
    finally:
        _cleanup(campaign_id, contact_id)


def test_paused_campaign_reschedules_instead_of_dropping(redis_client):
    campaign_id, contact_id, attempt_id = _setup("555-991-0003")
    session = _Session()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = CampaignStatus.PAUSED
    session.commit()
    session.close()

    try:
        scheduler = RecoveryScheduler(redis_client)
        queue = RedisStreamQueue(redis_client, "test:dispatch:calls3", "test:dispatch:workers3")
        job = RecoveryJob.new(
            attempt_id=attempt_id, contact_id=contact_id, campaign_id=campaign_id,
            attempt_number=2,
        )
        scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))

        dispatched = dispatch_due_recovery_jobs(scheduler, queue)

        assert dispatched == 0
        assert queue.redis.xlen("test:dispatch:calls3") == 0
        assert scheduler.pending_count() == 1  # rescheduled, not dropped
    finally:
        _cleanup(campaign_id, contact_id)


def test_outside_calling_window_reschedules_to_window_open(redis_client):
    campaign_id, contact_id, attempt_id = _setup(
        "555-991-0004", window=(dtime(9, 0), dtime(10, 0))
    )
    try:
        scheduler = RecoveryScheduler(redis_client)
        queue = RedisStreamQueue(redis_client, "test:dispatch:calls4", "test:dispatch:workers4")
        job = RecoveryJob.new(
            attempt_id=attempt_id, contact_id=contact_id, campaign_id=campaign_id,
            attempt_number=2,
        )
        # due "now" (23:00, well outside the 9-10am window)
        past_due = datetime.now(UTC).replace(hour=23, minute=0, second=0, microsecond=0)
        scheduler.schedule(job, past_due - timedelta(seconds=1))

        dispatched = dispatch_due_recovery_jobs(scheduler, queue, now=past_due)

        assert dispatched == 0
        assert scheduler.pending_count() == 1
    finally:
        _cleanup(campaign_id, contact_id)


def test_two_workers_dispatching_the_same_due_job_only_enqueue_once(redis_client):
    campaign_id, contact_id, attempt_id = _setup("555-991-0005", with_policy=False)
    try:
        scheduler = RecoveryScheduler(redis_client)
        queue = RedisStreamQueue(redis_client, "test:dispatch:calls5", "test:dispatch:workers5")
        job = RecoveryJob.new(
            attempt_id=attempt_id, contact_id=contact_id, campaign_id=campaign_id,
            attempt_number=2,
        )
        scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))

        first_pass = dispatch_due_recovery_jobs(scheduler, queue)
        second_pass = dispatch_due_recovery_jobs(scheduler, queue)  # simulates a 2nd worker's poll

        assert first_pass == 1
        assert second_pass == 0
        assert queue.redis.xlen("test:dispatch:calls5") == 1
    finally:
        _cleanup(campaign_id, contact_id)
