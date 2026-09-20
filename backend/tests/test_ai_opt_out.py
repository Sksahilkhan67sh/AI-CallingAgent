"""Opt-out detection and enforcement -- Checkpoint 04 Step 17, 50."""

from app.models.enums import ContactStatus
from app.services.ai.policy.engine import PolicyEngine
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_policy_engine_detects_opt_out_phrases_directly():
    policy = PolicyEngine()

    assert policy.detect_opt_out("Please stop calling me") is True
    assert policy.detect_opt_out("I'd like to learn more") is False


def test_opt_out_utterance_ends_conversation_and_suppresses_contact(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0010")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("Please don't call me again.")

    assert orchestrator.ended is True
    assert orchestrator.end_reason == "opt_out"
    assert contact.status == ContactStatus.CLOSED


def test_opt_out_writes_to_canonical_suppression_table(db_session):
    from sqlalchemy import select

    from app.models.suppression import Suppression

    _, contact, attempt = create_connected_call(db_session, phone="555-950-0011")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("remove me from your calling list")

    row = db_session.execute(
        select(Suppression).where(Suppression.contact_id == contact.id)
    ).scalar_one_or_none()
    assert row is not None
    assert row.phone_number == contact.normalized_phone_number


def test_opt_out_stops_further_ai_turns(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0012")
    orchestrator, stt, llm, tts, audio = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("stop calling me")
    calls_after_opt_out = len(llm.calls)

    orchestrator.handle_final_utterance("hello?")  # attempted further turn

    assert len(llm.calls) == calls_after_opt_out  # no additional AI turn processed


def test_opt_out_does_not_schedule_a_retry(db_session):
    """No retry mechanism exists yet (Checkpoint 03 explicitly deferred
    it) -- this just asserts the CallAttempt/Contact are left in a
    terminal state, not queued for anything."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0013")
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("do not call again")

    assert contact.status == ContactStatus.CLOSED
    assert attempt.state.value == "EndedNormally"
