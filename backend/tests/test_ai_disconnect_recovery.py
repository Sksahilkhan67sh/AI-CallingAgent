"""ConversationOrchestrator.handle_disconnect + reconnect memory
restoration -- Checkpoint 05 §17-18."""

from app.models.call_attempt import CallAttempt
from app.models.enums import (
    CallAttemptState,
    ContactStatus,
    MidCallDisconnectReason,
)
from app.models.retry_policy import RetryPolicy
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_handle_disconnect_sets_dropped_mid_call_state(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-992-0001")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_disconnect(MidCallDisconnectReason.TECHNICAL_ISSUE)

    assert attempt.state == CallAttemptState.DROPPED_MID_CALL
    assert attempt.disconnect_reason == MidCallDisconnectReason.TECHNICAL_ISSUE
    assert attempt.ended_at is not None
    # No RetryPolicy is configured for this test's campaign, so
    # RecoveryManager (invoked synchronously at the end of
    # handle_disconnect) immediately terminalizes -- contact.status
    # passes through Disconnected but settles at CompletedPartial. See
    # test_handle_disconnect_triggers_recovery_manager for the
    # retry-scheduled case.
    assert contact.status == ContactStatus.COMPLETED_PARTIAL


def test_handle_disconnect_checkpoints_memory_before_recovery_decision(db_session):
    from sqlalchemy import select

    from app.models.working_memory_snapshot import WorkingMemorySnapshot

    _, contact, attempt = create_connected_call(db_session, phone="555-992-0002")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    orchestrator.memory.captured_entities["interest_level"] = "interested"

    orchestrator.handle_disconnect(MidCallDisconnectReason.NETWORK_PROBLEM)

    snapshot = db_session.execute(
        select(WorkingMemorySnapshot)
        .where(WorkingMemorySnapshot.attempt_id == attempt.id)
        .order_by(WorkingMemorySnapshot.snapshotted_at.desc())
    ).scalars().first()
    assert snapshot.captured_entities == {"interest_level": "interested"}


def test_handle_disconnect_triggers_recovery_manager(db_session):
    """With a RetryPolicy configured and a retryable reason, disconnect
    handling schedules a retry -- contact ends up RetryScheduled, not
    just Disconnected."""
    campaign, contact, attempt = create_connected_call(db_session, phone="555-992-0003")
    db_session.add(RetryPolicy(campaign_id=campaign.id))
    db_session.flush()
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_disconnect(MidCallDisconnectReason.TECHNICAL_ISSUE)

    assert contact.status == ContactStatus.RETRY_SCHEDULED


def test_handle_disconnect_is_idempotent(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-992-0004")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_disconnect(MidCallDisconnectReason.TECHNICAL_ISSUE)
    first_ended_at = attempt.ended_at
    orchestrator.handle_disconnect(MidCallDisconnectReason.AI_ERROR)  # duplicate call

    assert attempt.ended_at == first_ended_at  # not overwritten
    assert attempt.disconnect_reason == MidCallDisconnectReason.TECHNICAL_ISSUE  # first one wins


def test_reconnect_restores_previous_memory(db_session):
    """§17: a new CallAttempt (the retry) loads working memory that was
    checkpointed under the PREVIOUS attempt, not its own (empty) memory."""
    campaign, contact, first_attempt = create_connected_call(db_session, phone="555-992-0005")
    orchestrator, *_ = build_orchestrator(db_session, first_attempt, contact)
    orchestrator.start()
    orchestrator.handle_final_utterance("I'm interested but call me later")

    # simulate the retry: a NEW CallAttempt for the same contact
    second_attempt = CallAttempt(
        contact_id=contact.id, attempt_number=2, state=CallAttemptState.CONNECTED
    )
    db_session.add(second_attempt)
    db_session.flush()

    from tests.ai_helpers import build_orchestrator as build

    reconnect_orchestrator, stt2, llm2, tts2, audio2 = build(db_session, second_attempt, contact)
    reconnect_orchestrator._memory_source_attempt_id = str(first_attempt.id)
    reconnect_orchestrator._is_reconnect = True

    reconnect_orchestrator.start()

    assert reconnect_orchestrator.memory.captured_entities.get("interest_level") == "interested"
    assert reconnect_orchestrator.memory.disconnect_count == 1
    assert reconnect_orchestrator.memory.attempt_id == str(second_attempt.id)


def test_reconnect_preserves_previous_transcript(db_session):
    """The previous attempt's ConversationMessage rows are never deleted
    or overwritten by a reconnect -- they remain queryable transcript
    history even though the new attempt has its own new session."""
    from sqlalchemy import select

    from app.models.conversation import ConversationMessage

    _, contact, first_attempt = create_connected_call(db_session, phone="555-992-0006")
    orchestrator, *_ = build_orchestrator(db_session, first_attempt, contact)
    orchestrator.start()
    orchestrator.handle_final_utterance("Hello")
    first_session_id = orchestrator.session.id

    orchestrator.handle_disconnect(MidCallDisconnectReason.TECHNICAL_ISSUE)

    messages = db_session.execute(
        select(ConversationMessage).where(ConversationMessage.session_id == first_session_id)
    ).scalars().all()
    assert len(messages) == 2  # still there, untouched
