"""End-to-end fake-provider conversation -- Checkpoint 04 Step 49, 52.

Drives a full conversation through audio -> STT -> orchestrator -> LLM
-> policy -> TTS -> audio, and checks every layer's resulting state,
not just that it didn't crash.
"""

import time

from app.models.conversation import CallEvent, ConversationMessage
from app.models.enums import CallAttemptState, ContactStatus
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_full_conversation_greeting_interest_qualification_close(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-960-0001")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    # Customer: "Hello" -> AI greets/moves toward discovery
    orchestrator.handle_final_utterance("Hello")
    assert orchestrator.ended is False

    # Customer: "I'm interested" -> captures interest, stays in Discovery
    orchestrator.handle_final_utterance("I'm interested")
    assert orchestrator.memory.captured_entities.get("interest_level") == "interested"

    # Customer: "Yes" -> moves toward Closing (no more required fields
    # configured in this test, so the very next affirmative closes out)
    orchestrator.handle_final_utterance("Yes")

    assert len(llm.calls) == 3
    assert len(tts.synthesized_texts) == 3
    assert len(audio.outbound_audio_log) == 3

    # transcript: 3 contact + 3 agent messages, in order
    from sqlalchemy import select

    messages = db_session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.session_id == orchestrator.session.id)
        .order_by(ConversationMessage.sequence)
    ).scalars().all()
    assert len(messages) == 6
    assert [m.role.value for m in messages] == [
        "contact", "agent", "contact", "agent", "contact", "agent",
    ]

    # CallEvents were logged for lifecycle
    events = db_session.execute(
        select(CallEvent).where(CallEvent.call_attempt_id == attempt.id)
    ).scalars().all()
    event_types = {e.event_type for e in events}
    assert "conversation_started" in event_types


def test_conversation_ends_with_correct_call_attempt_state(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-960-0002")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("no thanks, not interested")

    assert attempt.state == CallAttemptState.ENDED_NORMALLY
    assert attempt.ended_at is not None
    assert contact.status in (ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL)


def test_latency_instrumentation_measures_a_turn(db_session):
    """Step 52: validates instrumentation and timeout behavior against
    a mock -- does not claim production latency guarantees."""
    _, contact, attempt = create_connected_call(db_session, phone="555-960-0003")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    start = time.monotonic()
    orchestrator.handle_final_utterance("Hello")
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # a mock-provider turn should be near-instant
    assert len(llm.calls) == 1


def test_conversation_via_full_audio_stt_loop_not_direct_orchestrator_call(db_session):
    """Drives the conversation through the actual audio/STT plumbing
    (simulate_inbound_audio -> STT -> orchestrator), not by calling
    handle_final_utterance directly, to exercise the wiring itself."""
    _, contact, attempt = create_connected_call(db_session, phone="555-960-0004")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    audio.simulate_inbound_audio(b"\x00\x01\x02")
    assert stt.audio_chunks_received == 1

    stt.simulate_utterance("Hello there")

    assert len(llm.calls) == 1
    assert llm.calls[0].user_utterance == "Hello there"
