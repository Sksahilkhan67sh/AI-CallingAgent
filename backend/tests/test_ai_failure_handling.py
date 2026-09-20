"""Bounded failure handling -- Checkpoint 04 Steps 8, 20-21, 23, 38, 48.

No infinite retries anywhere; every failure path lands in a safe,
observable state.
"""

from tests.ai_helpers import build_orchestrator, create_connected_call


def test_llm_timeout_triggers_bounded_retry_then_fallback(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0030")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    llm.force_timeout = True

    orchestrator.handle_final_utterance("Hello")

    # Exactly bounded: MAX_LLM_RETRIES + 1 attempts, never more.
    from app.services.ai.conversation.orchestrator import MAX_LLM_RETRIES

    assert len(llm.calls) == MAX_LLM_RETRIES + 1
    assert orchestrator.ended is True
    assert orchestrator.end_reason == "end_call_polite"  # fallback's next_action ends the call


def test_llm_provider_error_falls_back_safely(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0031")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    llm.force_error = True

    orchestrator.handle_final_utterance("Hello")

    assert len(tts.synthesized_texts) == 1  # fallback response was spoken
    assert orchestrator.ended is True


def test_tts_timeout_does_not_send_stale_audio(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0032")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    tts.force_timeout = True

    orchestrator.handle_final_utterance("Hello")

    assert len(audio.outbound_audio_log) == 0
    assert orchestrator.ended is False  # a TTS hiccup on one turn doesn't end the call


def test_tts_error_is_handled_without_crashing(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0033")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    tts.force_error = True

    orchestrator.handle_final_utterance("Hello")  # must not raise

    assert len(audio.outbound_audio_log) == 0


def test_malformed_llm_response_triggers_regeneration_then_fallback(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0034")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    llm.force_malformed = True

    orchestrator.handle_final_utterance("Hello")

    from app.services.ai.conversation.orchestrator import MAX_RESPONSE_REGENERATIONS

    # Original attempt + bounded regenerations, never unbounded.
    assert len(llm.calls) == 1 + MAX_RESPONSE_REGENERATIONS
    assert len(tts.synthesized_texts) == 1
    assert tts.synthesized_texts[0].strip() != ""


def test_repeated_decline_ends_conversation(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0035")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("no thanks")
    assert orchestrator.ended is True  # a single "no thanks" already maps to END_CALL_POLITE


def test_repeated_unclear_ends_conversation_instead_of_looping(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0036")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("mumble mumble")  # unclear -> continues
    assert orchestrator.ended is False
    orchestrator.handle_final_utterance("mumble again")  # 2nd unclear -> bounded termination
    assert orchestrator.ended is True
    assert orchestrator.end_reason == "repeated_unclear"


def test_silence_handling_prompts_once_then_terminates(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0037")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_silence()
    assert orchestrator.ended is False
    assert tts.synthesized_texts == ["Are you still there?"]

    orchestrator.handle_silence()
    assert orchestrator.ended is True
    assert orchestrator.end_reason == "silence_timeout"


def test_maximum_turn_limit_is_enforced(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0038")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    from app.services.ai.conversation import orchestrator as orch_module

    orch_module.MAX_CONVERSATION_TURNS = 2
    try:
        orchestrator.handle_final_utterance("yes")
        orchestrator.handle_final_utterance("yes")
        assert orchestrator.ended is False
        orchestrator.handle_final_utterance("yes")
        assert orchestrator.ended is True
        assert orchestrator.end_reason == "max_turns_reached"
    finally:
        orch_module.MAX_CONVERSATION_TURNS = 40


def test_maximum_conversation_duration_is_enforced(db_session):
    from datetime import UTC, datetime, timedelta

    _, contact, attempt = create_connected_call(db_session, phone="555-950-0039")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()
    orchestrator.session.started_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()

    orchestrator.handle_final_utterance("hello")

    assert orchestrator.ended is True
    assert orchestrator.end_reason == "max_duration_reached"
