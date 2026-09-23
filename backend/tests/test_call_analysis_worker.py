"""Checkpoint 06 §32.D (LLM), §32.B (worker-side idempotency), §32.G
(retries), §32.H (worker crash/duplicate delivery).
"""

import uuid

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.services.analysis.admission import enqueue_call_analysis
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM
from app.services.analysis.worker import (
    AnalysisJobOutcome,
    process_claimed_job,
    process_one_analysis_job,
)
from app.services.phone import normalize_phone_number


def _admitted(db_session, *, phone, contact_text="I'm interested, tell me more"):
    campaign = Campaign(name="CP06 worker test")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
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
    if contact_text is not None:
        db_session.add(
            ConversationMessage(
                session_id=session.id, sequence=1, role=ConversationRole.AGENT, content="Hi!"
            )
        )
        db_session.add(
            ConversationMessage(
                session_id=session.id,
                sequence=2,
                role=ConversationRole.CONTACT,
                content=contact_text,
            )
        )
        db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)
    db_session.commit()
    return campaign, contact, attempt, session


def test_worker_completes_analysis_on_valid_llm_response(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0001")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.COMPLETED
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.summary is not None
    assert analysis.lead_score is not None
    assert analysis.prompt_version == get_settings().analysis_prompt_version
    assert analysis.input_message_count == 2
    assert len(llm.calls) == 1


def test_worker_handles_empty_transcript_without_calling_llm(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0002", contact_text=None)
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.COMPLETED
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.next_action.value == "manual_review"
    assert len(llm.calls) == 0  # cost control: no LLM call for nothing to analyze


def test_worker_on_llm_timeout_leaves_job_unacked_for_retry(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0003")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    llm.force_timeout = True

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_RETRIABLE
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.error_code == "AnalysisLLMTimeoutError"
    assert analysis.attempt_count == 1
    # Not acked -- still pending delivery in the consumer group.
    pending = redis_client.xpending(queue.stream_key, queue.group)
    assert pending["pending"] == 1


def test_worker_on_provider_error_retries_then_terminally_fails(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0004")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    llm.force_error = True

    read = queue.read_one("w1", block_ms=100)
    assert read is not None
    message_id, job = read

    max_attempts = get_settings().analysis_max_attempts
    outcome = None
    for _ in range(max_attempts):
        outcome = process_claimed_job(db_session, queue, llm, message_id, job)
        db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_TERMINAL
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.attempt_count == max_attempts
    pending = redis_client.xpending(queue.stream_key, queue.group)
    assert pending["pending"] == 0  # acked once terminally failed -- never retried again


def test_malformed_llm_output_does_not_get_persisted_as_valid(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0005")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    llm.force_malformed = True

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_RETRIABLE
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.summary is None  # never fabricated a default result


def test_already_completed_analysis_is_a_no_op_on_redelivery(db_session, redis_client):
    """§6/§32.B/§32.H -- duplicate delivery of an already-COMPLETED
    analysis (e.g. a crash between persistence and ack) must not
    reprocess or re-call the LLM."""
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0006")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()

    read = queue.read_one("w1", block_ms=100)
    assert read is not None
    message_id, job = read
    outcome = process_claimed_job(db_session, queue, llm, message_id, job)
    db_session.commit()
    assert outcome == AnalysisJobOutcome.COMPLETED
    assert len(llm.calls) == 1

    # Simulate redelivery of the same message (crash-before-ack replay).
    outcome_again = process_claimed_job(db_session, queue, llm, message_id, job)
    db_session.commit()

    assert outcome_again == AnalysisJobOutcome.ALREADY_PROCESSED
    assert len(llm.calls) == 1  # LLM never called a second time


def test_reclaim_stale_redelivers_an_unacked_job_to_another_worker(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="555-300-0007")
    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    llm.force_timeout = True

    # First worker reads it, fails transiently, leaves it unacked.
    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="worker-a")
    db_session.commit()
    assert outcome == AnalysisJobOutcome.FAILED_RETRIABLE

    # A second worker reclaims it after the idle window and succeeds.
    llm.force_timeout = False
    reclaimed = queue.reclaim_stale("worker-b", idle_ms=0)
    assert len(reclaimed) == 1
    message_id, job = reclaimed[0]
    outcome = process_claimed_job(db_session, queue, llm, message_id, job)
    db_session.commit()

    assert outcome == AnalysisJobOutcome.COMPLETED
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.attempt_count == 2  # one failed attempt + one successful


def test_missing_analysis_row_is_acked_and_not_retried(db_session, redis_client):
    """A job referencing an analysis_id that no longer exists (data
    problem) must be acked, not retried forever."""
    from app.services.analysis.job import AnalysisJob

    queue = get_analysis_queue()
    llm = FakeAnalysisLLM()
    job = AnalysisJob.new(
        analysis_id=uuid.uuid4(),
        call_attempt_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        conversation_session_id=None,
    )
    message_id = queue.enqueue(job)

    outcome = process_claimed_job(db_session, queue, llm, message_id, job)
    db_session.commit()

    assert outcome == AnalysisJobOutcome.NOT_ELIGIBLE
    pending = redis_client.xpending(queue.stream_key, queue.group)
    assert pending["pending"] == 0
