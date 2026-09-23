"""Checkpoint 06 §32.J -- full lifecycle integration test, and the
§0/§34 invariant that analysis (failure or success) never touches
call/retry/campaign infrastructure.
"""

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import (
    CallEvent,
    ConversationMessage,
    ConversationRole,
    ConversationSession,
)
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.services.analysis.admission import enqueue_call_analysis
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM
from app.services.analysis.worker import AnalysisJobOutcome, process_one_analysis_job
from app.services.phone import normalize_phone_number


def test_full_lifecycle_terminal_call_to_persisted_analysis(db_session, redis_client):
    # 1-4: campaign/contact/terminal call attempt/conversation session
    campaign = Campaign(name="CP06 integration")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-500-0001",
        normalized_phone_number=normalize_phone_number("555-500-0001"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()

    # 5: realistic conversation messages
    turns = [
        (ConversationRole.AGENT, "Hi, calling about our new plan -- got a minute?"),
        (ConversationRole.CONTACT, "Sure, go ahead."),
        (ConversationRole.AGENT, "Great -- it includes unlimited data. Interested?"),
        (ConversationRole.CONTACT, "Yes, I'm interested, how much does it cost though?"),
    ]
    for i, (role, content) in enumerate(turns, start=1):
        db_session.add(
            ConversationMessage(session_id=session.id, sequence=i, role=role, content=content)
        )
    db_session.flush()

    # 6: trigger analysis admission (the same call orchestrator/recovery
    # manager make at a terminal transition)
    enqueue_call_analysis(db_session, attempt, contact)
    db_session.commit()

    # 7: verify Redis analysis job
    queue = get_analysis_queue()
    assert redis_client.xlen(queue.stream_key) == 1

    # 8-9: start analysis worker, process job
    llm = FakeAnalysisLLM()
    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="integration-worker")
    db_session.commit()
    assert outcome == AnalysisJobOutcome.COMPLETED

    # 9: verify PostgreSQL CallAnalysis
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.summary
    assert analysis.lead_score is not None
    assert analysis.input_message_count == 4

    # 10: verify event/audit
    events = {
        e.event_type
        for e in db_session.query(CallEvent).filter(CallEvent.call_attempt_id == attempt.id).all()
    }
    assert "ANALYSIS_QUEUED" in events
    assert "ANALYSIS_COMPLETED" in events

    # 11: verify job acknowledgement
    pending = redis_client.xpending(queue.stream_key, queue.group)
    assert pending["pending"] == 0

    # 12: verify duplicate job does not duplicate analysis
    enqueue_call_analysis(db_session, attempt, contact)  # simulates a duplicate terminal event
    db_session.commit()
    assert redis_client.xlen(queue.stream_key) == 1  # unchanged -- no second job
    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 1
    )


def test_analysis_failure_does_not_touch_call_or_campaign_state(db_session, redis_client):
    """§0/§34: analysis failure must never redial, invoke RecoveryManager,
    create another CallAttempt, alter suppression, or change campaign/
    call terminal state -- it only ever mutates its own CallAnalysis row."""
    campaign = Campaign(name="CP06 isolation test")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-500-0002",
        normalized_phone_number=normalize_phone_number("555-500-0002"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        ConversationMessage(
            session_id=session.id, sequence=1, role=ConversationRole.CONTACT, content="hello"
        )
    )
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)
    db_session.commit()

    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    llm.force_error = True

    contact_attempt_count_before = contact.attempt_count
    contact_status_before = contact.status
    call_attempt_state_before = attempt.state
    call_attempt_count_before = (
        db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).count()
    )

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()
    assert outcome == AnalysisJobOutcome.FAILED_RETRIABLE

    db_session.refresh(contact)
    db_session.refresh(attempt)
    assert contact.attempt_count == contact_attempt_count_before
    assert contact.status == contact_status_before
    assert attempt.state == call_attempt_state_before
    assert (
        db_session.query(CallAttempt).filter(CallAttempt.contact_id == contact.id).count()
        == call_attempt_count_before
    )
    assert redis_client.zcard("recovery:scheduled") == 0
