"""ConversationOrchestrator unit tests -- Checkpoint 04 Step 45."""

from app.models.conversation import ConversationMessage
from app.models.enums import ConversationPhase, ConversationSessionStatus
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_conversation_initialization_creates_session(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)

    orchestrator.start()

    assert orchestrator.session is not None
    assert orchestrator.session.status == ConversationSessionStatus.ACTIVE
    assert orchestrator.session.call_attempt_id == attempt.id
    assert audio.is_connected() is True


def test_conversation_state_transitions_on_affirmative(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    assert orchestrator.memory.script_progress.phase == ConversationPhase.OPENING

    orchestrator.handle_final_utterance("Yes, sounds good")

    assert orchestrator.memory.script_progress.phase == ConversationPhase.DISCOVERY


def test_user_utterance_creates_transcript_message(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Hello there")

    from sqlalchemy import select

    messages = db_session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.session_id == orchestrator.session.id)
        .order_by(ConversationMessage.sequence)
    ).scalars().all()
    assert len(messages) == 2  # contact utterance + agent response
    assert messages[0].role.value == "contact"
    assert messages[0].content == "Hello there"
    assert messages[1].role.value == "agent"


def test_memory_updates_from_captured_entities(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Yes, I'm interested")

    assert orchestrator.memory.captured_entities.get("interest_level") == "interested"


def test_context_construction_is_bounded(db_session):
    from app.services.ai.conversation.context import MAX_RECENT_MESSAGES, build_turn_context
    from app.services.ai.memory.schema import WorkingMemory

    memory = WorkingMemory(attempt_id="a", contact_id="c")
    long_history = [f"turn {i}" for i in range(50)]

    context = build_turn_context(
        agent_config=None,
        memory=memory,
        brand_name="Test Co",
        recent_message_texts=long_history,
        user_utterance="hello",
    )

    # bounded to MAX_RECENT_MESSAGES + 1 summary placeholder line
    assert len(context.recent_messages) == MAX_RECENT_MESSAGES + 1
    assert "summarized" in context.recent_messages[0]


def test_policy_enforcement_ends_call_on_negative_intent(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("no thanks, not interested")

    assert orchestrator.ended is True


def test_llm_is_invoked_with_turn_context(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Hello")

    assert len(llm.calls) == 1
    assert llm.calls[0].user_utterance == "Hello"


def test_response_validation_rejects_empty_response_and_falls_back(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    llm.force_malformed = True

    orchestrator.handle_final_utterance("Hello")

    assert len(tts.synthesized_texts) == 1
    assert tts.synthesized_texts[0] != ""  # fallback response was used, not the empty one


def test_tts_is_invoked_and_audio_sent(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Hello")

    assert len(tts.synthesized_texts) == 1
    assert len(audio.outbound_audio_log) == 1


def test_final_transcript_persists_incrementally_not_at_end(db_session):
    from sqlalchemy import select

    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Hello")
    # session is still active -- messages from turn 1 must already be
    # visible, not buffered until conversation end (Step 10)
    assert orchestrator.ended is False
    messages = db_session.execute(
        select(ConversationMessage).where(ConversationMessage.session_id == orchestrator.session.id)
    ).scalars().all()
    assert len(messages) == 2


def test_conversation_completion_marks_session_ended(db_session):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("no thanks")

    assert orchestrator.session.status == ConversationSessionStatus.ENDED
    assert orchestrator.session.ended_at is not None


def test_partial_transcript_does_not_create_a_message(db_session):
    from sqlalchemy import select

    _, contact, attempt = create_connected_call(db_session)
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    stt.simulate_utterance("final text", partials=["fi", "fin", "final"])

    messages = db_session.execute(
        select(ConversationMessage).where(ConversationMessage.session_id == orchestrator.session.id)
    ).scalars().all()
    contact_messages = [m for m in messages if m.role.value == "contact"]
    assert len(contact_messages) == 1
    assert contact_messages[0].content == "final text"
