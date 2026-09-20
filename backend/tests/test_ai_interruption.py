"""Barge-in / interruption and stale-response protection -- Checkpoint
04 Steps 24-25, 46, 51.

Because FakeLLM/FakeTTS are synchronous (they return instantly), a real
race ("customer interrupts WHILE the AI is mid-generation") is
simulated via a hook the fakes call at the moment generate_response/
synthesize would normally be in flight -- the same technique
Checkpoint 03 used to simulate provider races deterministically.
"""

from app.models.enums import Intent, NextAction
from app.services.ai.schemas import Entities, StructuredOutput
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_barge_in_stops_outbound_audio(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0020")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.on_barge_in()

    assert audio.interrupt_count == 1


def test_stale_llm_response_is_discarded_after_barge_in(db_session):
    """Turn A's LLM call is in flight; a barge-in happens (bumping the
    generation) before turn A's output would be spoken. Turn A's output
    must never reach TTS/audio."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0021")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    def interrupt_mid_generation(context):
        # Simulates the customer starting to speak again while this
        # LLM call was still "in flight" for the previous turn.
        orchestrator.on_barge_in()
        return StructuredOutput(
            intent=Intent.AFFIRMATIVE,
            entities=Entities(),
            next_action=NextAction.CONTINUE_SCRIPT,
            response_text="This is turn A's stale response",
        )

    llm.generate_response = interrupt_mid_generation  # type: ignore[method-assign]

    orchestrator.handle_final_utterance("first question")

    assert "stale" not in " ".join(tts.synthesized_texts)
    assert len(tts.synthesized_texts) == 0  # turn A never reached TTS at all


def test_new_turn_after_interruption_is_processed_normally(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0022")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.on_barge_in()
    orchestrator.handle_final_utterance("actually, yes I'm interested")

    assert len(tts.synthesized_texts) == 1
    assert len(llm.calls) == 1


def test_stale_response_discarded_during_tts_synthesis(db_session):
    """The response passed LLM/validation, but a barge-in happened
    while TTS was synthesizing it -- must still be discarded before
    reaching audio output."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0023")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    def interrupt_mid_synthesis(text):
        orchestrator.on_barge_in()
        return text.encode()

    tts.synthesize = interrupt_mid_synthesis  # type: ignore[method-assign]

    orchestrator.handle_final_utterance("hello")

    assert len(audio.outbound_audio_log) == 0  # discarded before reaching audio
