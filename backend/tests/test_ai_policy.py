"""PolicyEngine unit tests -- Checkpoint 04 Step 16, 18."""

from app.models.enums import ConversationPhase, Intent, NextAction
from app.services.ai.policy.engine import MAX_RESPONSE_CHARS, PolicyEngine
from app.services.ai.schemas import Entities, StructuredOutput


def _output(intent=Intent.AFFIRMATIVE, next_action=NextAction.CONTINUE_SCRIPT, text="ok", **kw):
    return StructuredOutput(
        intent=intent, entities=Entities(), next_action=next_action, response_text=text, **kw
    )


def test_validate_response_rejects_empty_text():
    policy = PolicyEngine()
    assert policy.validate_response(_output(text="")) is False
    assert policy.validate_response(_output(text="   ")) is False


def test_validate_response_rejects_oversized_text():
    policy = PolicyEngine()
    output = _output(text="x" * (MAX_RESPONSE_CHARS + 1))
    assert policy.validate_response(output) is False


def test_validate_response_accepts_normal_text():
    policy = PolicyEngine()
    assert policy.validate_response(_output(text="Sounds good, let's continue.")) is True


def test_opt_out_decision_overrides_everything_else():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.DISCOVERY,
        output=_output(intent=Intent.AFFIRMATIVE, requires_suppression=True),
        utterance="yes",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=True,
    )
    assert decision.opt_out_detected is True
    assert decision.should_terminate is True
    assert decision.next_phase == ConversationPhase.WRAP_UP


def test_opening_affirmative_routes_to_discovery():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.OPENING,
        output=_output(intent=Intent.AFFIRMATIVE),
        utterance="yes",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=True,
    )
    assert decision.next_phase == ConversationPhase.DISCOVERY
    assert decision.should_terminate is False


def test_discovery_affirmative_with_no_pending_fields_routes_to_closing():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.DISCOVERY,
        output=_output(intent=Intent.AFFIRMATIVE),
        utterance="yes",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=False,
    )
    assert decision.next_phase == ConversationPhase.CLOSING


def test_discovery_objection_routes_to_objection_handling():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.DISCOVERY,
        output=_output(intent=Intent.OBJECTION, next_action=NextAction.HANDLE_OBJECTION),
        utterance="that seems expensive",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=True,
    )
    assert decision.next_phase == ConversationPhase.OBJECTION_HANDLING


def test_wrap_up_is_terminal_with_no_outgoing_routes():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.WRAP_UP,
        output=_output(intent=Intent.QUESTION),
        utterance="one more thing",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=False,
    )
    assert decision.next_phase == ConversationPhase.WRAP_UP


def test_request_human_routes_to_wrap_up():
    policy = PolicyEngine()
    decision = policy.decide(
        current_phase=ConversationPhase.DISCOVERY,
        output=_output(intent=Intent.REQUEST_HUMAN, next_action=NextAction.ESCALATE_TO_HUMAN_OFFER),
        utterance="can I speak to a person",
        consecutive_declines=0,
        consecutive_unclear=0,
        has_pending_required_fields=True,
    )
    assert decision.next_phase == ConversationPhase.WRAP_UP
    assert decision.should_terminate is True
