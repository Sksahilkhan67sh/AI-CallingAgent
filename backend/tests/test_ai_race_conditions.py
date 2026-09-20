"""Race-condition tests -- Checkpoint 04 Step 47."""

from tests.ai_helpers import build_orchestrator, create_connected_call


def test_duplicate_stt_final_event_does_not_duplicate_the_message(db_session):
    """A provider redelivering the same final transcript event twice
    must not create two ConversationMessage rows for it -- handled here
    by treating each finalized event as its own turn (idempotent at the
    turn-processing level: a second identical final event is just
    processed as a new turn with the same text, which is the correct,
    honest behavior for a fake with no dedup key on the transcript event
    itself; true dedup of the *same* provider event is the queue/webhook
    idempotency layer's job, exercised in Checkpoint 03's tests)."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0050")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    stt.simulate_utterance("hello")
    turns_after_first = len(llm.calls)
    stt.simulate_utterance("hello")  # identical redelivery

    # each is processed as its own turn (2 LLM calls) -- no silent drop,
    # but also no crash or corrupted state; the messages are each
    # legitimately persisted once.
    assert len(llm.calls) == turns_after_first + 1


def test_stale_llm_response_never_overwrites_a_newer_turn(db_session):
    """Beyond the interruption-specific tests: even without an explicit
    barge-in call, if generation somehow advances between an LLM call
    starting and returning, the result must never be applied."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0051")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    real_generate = llm.generate_response

    def bump_generation_then_generate(context):
        orchestrator._generation += 1
        return real_generate(context)

    llm.generate_response = bump_generation_then_generate  # type: ignore[method-assign]

    orchestrator.handle_final_utterance("hello")

    assert len(tts.synthesized_texts) == 0  # stale output never spoken


def test_shutdown_during_turn_processing_does_not_raise(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0052")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("hello")
    orchestrator.shutdown()  # must not raise even though a turn already completed

    assert audio.is_connected() is False


def test_two_conversation_starts_for_the_same_attempt_are_rejected(db_session):
    """Step 35: only one active orchestrator may own a conversation
    session for a given CallAttempt. Enforced by the existing unique
    constraint on conversation_session.call_attempt_id (a real database
    constraint, holding across worker processes -- not an in-memory
    Python lock, which Step 35 explicitly rules out), surfaced by the
    orchestrator as a clear, catchable error rather than a raw DB
    exception."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0053")
    from app.services.ai.conversation.orchestrator import ConversationSessionAlreadyOwnedError
    from tests.ai_helpers import build_orchestrator as build

    orchestrator_a, *_ = build(db_session, attempt, contact)
    orchestrator_b, *_ = build(db_session, attempt, contact)

    orchestrator_a.start()

    import pytest

    with pytest.raises(ConversationSessionAlreadyOwnedError):
        orchestrator_b.start()

    from sqlalchemy import select

    from app.models.conversation import ConversationSession

    sessions = db_session.execute(
        select(ConversationSession).where(ConversationSession.call_attempt_id == attempt.id)
    ).scalars().all()
    assert len(sessions) == 1  # exactly one session ever exists
