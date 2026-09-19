"""Checkpoint 03 Step 33: multiple workers racing to process the same
(contact_id, attempt_number) must result in exactly ONE CallAttempt and
exactly one real provider call -- never two, regardless of how many
workers/queue deliveries raced for it.

Uses real threads and independent DB sessions/connections -- the same
pattern Checkpoint 01A's concurrency test established -- because the
point is to exercise the database's own protection under genuine
concurrent access, not just sequential application logic.
"""

import os
import threading

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker

_engine = create_engine(os.environ["PRIMARY_DB_URL"])
_Session = sessionmaker(bind=_engine)


def test_concurrent_workers_create_exactly_one_call_attempt(redis_client, provider):
    setup_session = _Session()
    campaign = Campaign(name="Concurrency dialer test", status=CampaignStatus.ACTIVE)
    setup_session.add(campaign)
    setup_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-700-0001",
        normalized_phone_number=normalize_phone_number("555-700-0001"),
        status=ContactStatus.PENDING,
    )
    setup_session.add(contact)
    setup_session.commit()
    campaign_id, contact_id = campaign.id, contact.id
    setup_session.close()

    queue = RedisStreamQueue(redis_client, "test:concurrency:calls", "test:concurrency:workers")
    admission = AdmissionController(
        redis_client,
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )
    breaker = CircuitBreaker(redis_client, provider.name)

    # The same job delivered twice -- simulating two workers racing on
    # a duplicate delivery of the same logical unit of work.
    job = DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=1)
    queue.enqueue(job)
    queue.enqueue(job)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        session = _Session()
        try:
            barrier.wait(timeout=5)
            outcome = process_one_job(
                session, queue, admission, provider, breaker, consumer_name=name
            )
            session.commit()
            outcomes.append(outcome)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    verify_session = _Session()
    attempts = verify_session.execute(
        select(CallAttempt).where(CallAttempt.contact_id == contact_id)
    ).scalars().all()
    assert len(attempts) == 1  # exactly one CallAttempt, never two

    provider_calls_placed = len(provider._calls)
    assert provider_calls_placed == 1  # create_outbound_call effectively happened once

    # cleanup
    verify_session.query(CallAttempt).filter_by(contact_id=contact_id).delete()
    verify_session.query(Contact).filter_by(id=contact_id).delete()
    verify_session.query(Campaign).filter_by(id=campaign_id).delete()
    verify_session.commit()
    verify_session.close()
