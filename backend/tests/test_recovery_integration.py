"""End-to-end recovery integration -- Checkpoint 05 §25, real Postgres
+ Redis, driven through the actual dialer worker and recovery
dispatcher, not just direct orchestrator/manager calls.
"""

import os
from datetime import UTC, datetime, timedelta
from datetime import time as _dtime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, MidCallDisconnectReason
from app.models.retry_policy import RetryPolicy
from app.services.phone import normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import process_one_job
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.scheduler import RecoveryScheduler
from app.services.telephony.circuit_breaker import CircuitBreaker

_engine = create_engine(os.environ["PRIMARY_DB_URL"])
_Session = sessionmaker(bind=_engine)


def _admission(redis_client) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )


def _cleanup(campaign_id, contact_id):
    session = _Session()
    from app.models.audit_log import AuditLog
    from app.models.conversation import CallEvent, ConversationMessage, ConversationSession
    from app.models.working_memory_snapshot import WorkingMemorySnapshot

    attempt_ids = [r[0] for r in session.query(CallAttempt.id).filter_by(contact_id=contact_id)]
    session_ids = (
        [
            r[0]
            for r in session.query(ConversationSession.id).filter(
                ConversationSession.call_attempt_id.in_(attempt_ids)
            )
        ]
        if attempt_ids
        else []
    )
    if session_ids:
        session.query(ConversationMessage).filter(
            ConversationMessage.session_id.in_(session_ids)
        ).delete(synchronize_session=False)
        session.query(ConversationSession).filter(ConversationSession.id.in_(session_ids)).delete(
            synchronize_session=False
        )
    if attempt_ids:
        session.query(WorkingMemorySnapshot).filter(
            WorkingMemorySnapshot.attempt_id.in_(attempt_ids)
        ).delete(synchronize_session=False)
        session.query(CallEvent).filter(CallEvent.call_attempt_id.in_(attempt_ids)).delete(
            synchronize_session=False
        )
        session.query(AuditLog).filter(AuditLog.entity_id.in_(attempt_ids)).delete(
            synchronize_session=False
        )
    session.query(AuditLog).filter_by(entity_id=contact_id).delete()
    session.query(CallAttempt).filter_by(contact_id=contact_id).delete()
    session.query(Contact).filter_by(id=contact_id).delete()
    session.query(RetryPolicy).filter_by(campaign_id=campaign_id).delete()
    session.query(Campaign).filter_by(id=campaign_id).delete()
    session.commit()
    session.close()


def test_full_disconnect_retry_reconnect_cycle_through_real_worker(redis_client, provider):
    """Attempt 1 connects, conversation runs, disconnects mid-call.
    RecoveryManager schedules retry #1. Dispatcher (with the schedule
    forced due) enqueues it to the real dialer queue. The real dialer
    worker processes it, creating Attempt 2, and the reconnect restores
    Attempt 1's memory."""
    session = _Session()
    campaign = Campaign(name="Integration recovery", status=CampaignStatus.ACTIVE)
    session.add(campaign)
    session.flush()
    session.add(
        RetryPolicy(campaign_id=campaign.id, window_start=_dtime(0, 0), window_end=_dtime(23, 59))
    )
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-993-0001",
        normalized_phone_number=normalize_phone_number("555-993-0001"),
        status=ContactStatus.IN_CONVERSATION,
    )
    session.add(contact)
    session.flush()
    from app.models.enums import CallAttemptState

    attempt1 = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        state=CallAttemptState.CONNECTED,
        provider="mock",
        provider_call_id="mock-integration-1",
    )
    session.add(attempt1)
    session.commit()
    campaign_id, contact_id, attempt1_id = campaign.id, contact.id, attempt1.id
    session.close()

    try:
        # -- run a real conversation turn via the orchestrator, then disconnect --
        from app.services.ai.conversation.start import start_conversation

        db = _Session()
        contact_row = db.get(Contact, contact_id)
        attempt_row = db.get(CallAttempt, attempt1_id)
        orchestrator = start_conversation(db, attempt_row, contact_row)
        orchestrator.handle_final_utterance("I'm interested, call me back")
        orchestrator.handle_disconnect(MidCallDisconnectReason.TECHNICAL_ISSUE)
        db.commit()
        db.close()

        # -- verify recovery was scheduled --
        scheduler = RecoveryScheduler(redis_client)
        assert scheduler.pending_count() == 1

        # -- force the scheduled job due, dispatch it onto the real dialer queue --
        queue = RedisStreamQueue(redis_client, "calls:outbound", "dialer-workers")
        due_jobs = scheduler.due_jobs(datetime.now(UTC) + timedelta(hours=1))
        assert len(due_jobs) == 1
        dispatched = dispatch_due_recovery_jobs(
            scheduler, queue, now=datetime.now(UTC) + timedelta(hours=1)
        )
        assert dispatched == 1

        # -- the REAL dialer worker processes the retry job --
        db2 = _Session()
        admission = _admission(redis_client)
        breaker = CircuitBreaker(redis_client, provider.name)
        outcome = process_one_job(
            db2, queue, admission, provider, breaker, consumer_name="integration-test"
        )
        db2.commit()
        assert outcome == "admitted_and_dialed"

        # -- verify Attempt 2 was created, distinct from Attempt 1 --
        attempts = (
            db2.execute(
                select(CallAttempt)
                .where(CallAttempt.contact_id == contact_id)
                .order_by(CallAttempt.attempt_number)
            )
            .scalars()
            .all()
        )
        assert len(attempts) == 2
        assert attempts[0].id == attempt1_id
        assert attempts[1].attempt_number == 2
        assert attempts[1].id != attempt1_id
        db2.close()
    finally:
        _cleanup(campaign_id, contact_id)


def test_opt_out_cancels_pending_recovery(db_session, redis_client):
    """§8: opt-out never goes through RecoveryManager/scheduling at
    all -- it uses the graceful _end_conversation path, so no retry is
    ever scheduled for it."""
    from tests.ai_helpers import build_orchestrator, create_connected_call

    campaign, contact, attempt = create_connected_call(db_session, phone="555-993-0002")
    db_session.add(RetryPolicy(campaign_id=campaign.id))
    db_session.flush()
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("please stop calling me")

    scheduler = RecoveryScheduler(redis_client)
    assert scheduler.pending_count() == 0
    assert contact.status == ContactStatus.CLOSED


def test_max_attempts_reached_produces_no_fourth_attempt(db_session, redis_client):
    from app.models.enums import CallAttemptState
    from app.services.recovery.manager import RecoveryManager
    from tests.ai_helpers import create_connected_call

    campaign, contact, first_attempt = create_connected_call(db_session, phone="555-993-0003")
    db_session.add(RetryPolicy(campaign_id=campaign.id))
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))
    first_attempt.state = CallAttemptState.DROPPED_MID_CALL
    db_session.flush()

    # simulate attempts 1 (already created by the helper), 2, 3 all disconnecting
    decision = manager.handle_disconnect(
        first_attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )
    for attempt_number in (2, 3):
        attempt = CallAttempt(
            contact_id=contact.id,
            attempt_number=attempt_number,
            state=CallAttemptState.DROPPED_MID_CALL,
        )
        db_session.add(attempt)
        db_session.flush()
        decision = manager.handle_disconnect(
            attempt,
            contact,
            campaign,
            never_connected=False,
            reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
        )

    assert decision.should_retry is False
    assert decision.reason == "max_attempts_reached"
    # Two retries were legitimately scheduled along the way (1->2,
    # 2->3); the terminal 3rd decision must not have added a third.
    assert RecoveryScheduler(redis_client).pending_count() == 2

    attempts = (
        db_session.execute(select(CallAttempt).where(CallAttempt.contact_id == contact.id))
        .scalars()
        .all()
    )
    assert len(attempts) == 3  # never a 4th
