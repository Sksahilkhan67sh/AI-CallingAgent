"""Deterministic policy layer -- Checkpoint 04 Step 16.

Everything here is a fixed rule, not a model decision. The LLM proposes
(intent, next_action, response_text); this module is what actually
decides phase transitions, termination, and opt-out enforcement. Per
Step 17, opt-out detection does not rely on the LLM alone -- a
deterministic keyword check runs against the raw utterance as a
backstop to whatever the structured output says.

This module never writes to the `suppression` table itself -- it only
decides *that* suppression is required; the orchestrator is what calls
the existing SuppressionRepository (Checkpoint 01/02), so there is
still exactly one place that writes to that table.
"""

from dataclasses import dataclass

from app.models.enums import ConversationPhase, Intent, NextAction
from app.services.ai.schemas import StructuredOutput

MAX_RESPONSE_CHARS = 1000  # Step 58 -- bound LLM response size
MAX_CONSECUTIVE_DECLINES = 2  # Step 8 -- don't persist past a second decline
MAX_CONSECUTIVE_UNCLEAR = 2  # Step 8 -- don't loop on unclear utterances

# Deterministic backstop for opt-out phrases -- intentionally the same
# small phrase set FakeLLM uses to *simulate* model behavior, but this
# check runs independently of what the model actually returned.
_OPT_OUT_PHRASES = ("stop calling", "don't call", "do not call", "remove me", "no more calls")

# Conversation-Flow.md §4 intent-to-phase routing table.
_ROUTING: dict[ConversationPhase, dict[Intent, ConversationPhase]] = {
    ConversationPhase.OPENING: {
        Intent.AFFIRMATIVE: ConversationPhase.DISCOVERY,
        Intent.QUESTION: ConversationPhase.DISCOVERY,
        Intent.NEGATIVE: ConversationPhase.WRAP_UP,
        Intent.END_CALL: ConversationPhase.WRAP_UP,
        Intent.REQUEST_HUMAN: ConversationPhase.WRAP_UP,
        Intent.REQUEST_CALLBACK: ConversationPhase.WRAP_UP,
    },
    ConversationPhase.DISCOVERY: {
        Intent.OBJECTION: ConversationPhase.OBJECTION_HANDLING,
        Intent.NEGATIVE: ConversationPhase.WRAP_UP,
        Intent.END_CALL: ConversationPhase.WRAP_UP,
        Intent.REQUEST_HUMAN: ConversationPhase.WRAP_UP,
        Intent.REQUEST_CALLBACK: ConversationPhase.WRAP_UP,
        # affirmative/question/off_topic/unclear: stay in Discovery
        # (handled by the default-stays-in-phase fallback below)
    },
    ConversationPhase.OBJECTION_HANDLING: {
        Intent.NEGATIVE: ConversationPhase.WRAP_UP,
        Intent.END_CALL: ConversationPhase.WRAP_UP,
        Intent.REQUEST_HUMAN: ConversationPhase.WRAP_UP,
        Intent.REQUEST_CALLBACK: ConversationPhase.WRAP_UP,
        # affirmative routes to Discovery or Closing depending on
        # whether required fields remain -- resolved by the orchestrator
        # (it knows script_progress), not this static table.
    },
    ConversationPhase.CLOSING: {
        Intent.AFFIRMATIVE: ConversationPhase.WRAP_UP,
        Intent.NEGATIVE: ConversationPhase.WRAP_UP,
        Intent.END_CALL: ConversationPhase.WRAP_UP,
        Intent.REQUEST_HUMAN: ConversationPhase.WRAP_UP,
        Intent.REQUEST_CALLBACK: ConversationPhase.WRAP_UP,
    },
    ConversationPhase.WRAP_UP: {},  # terminal -- no outgoing transitions
}

# next_action values that unconditionally mean "the call should end
# after this turn" regardless of phase-routing.
_TERMINAL_NEXT_ACTIONS = {
    NextAction.END_CALL_POLITE,
    NextAction.END_CALL_GOAL_MET,
}


@dataclass
class PolicyDecision:
    next_phase: ConversationPhase
    should_terminate: bool
    termination_reason: str | None = None
    opt_out_detected: bool = False


class PolicyEngine:
    def detect_opt_out(self, utterance: str) -> bool:
        lowered = utterance.lower()
        return any(phrase in lowered for phrase in _OPT_OUT_PHRASES)

    def validate_response(self, output: StructuredOutput) -> bool:
        """Step 18 -- reject responses that violate basic safety rules
        before they ever reach TTS."""
        if not output.response_text.strip():
            return False
        if len(output.response_text) > MAX_RESPONSE_CHARS:
            return False
        return True

    def decide(
        self,
        *,
        current_phase: ConversationPhase,
        output: StructuredOutput,
        utterance: str,
        consecutive_declines: int,
        consecutive_unclear: int,
        has_pending_required_fields: bool,
    ) -> PolicyDecision:
        opt_out = output.requires_suppression or self.detect_opt_out(utterance)
        if opt_out:
            return PolicyDecision(
                next_phase=ConversationPhase.WRAP_UP,
                should_terminate=True,
                termination_reason="opt_out",
                opt_out_detected=True,
            )

        if output.next_action in _TERMINAL_NEXT_ACTIONS:
            return PolicyDecision(
                next_phase=ConversationPhase.WRAP_UP,
                should_terminate=True,
                termination_reason=output.next_action.value,
            )

        if (
            output.intent == Intent.NEGATIVE
            and consecutive_declines >= MAX_CONSECUTIVE_DECLINES
        ):
            return PolicyDecision(
                next_phase=ConversationPhase.WRAP_UP,
                should_terminate=True,
                termination_reason="repeated_decline",
            )

        if (
            output.intent == Intent.UNCLEAR
            and consecutive_unclear >= MAX_CONSECUTIVE_UNCLEAR
        ):
            return PolicyDecision(
                next_phase=ConversationPhase.WRAP_UP,
                should_terminate=True,
                termination_reason="repeated_unclear",
            )

        next_phase = self._route(current_phase, output.intent, has_pending_required_fields)
        should_terminate = next_phase == ConversationPhase.WRAP_UP
        return PolicyDecision(
            next_phase=next_phase,
            should_terminate=should_terminate,
            termination_reason="wrap_up" if should_terminate else None,
        )

    def _route(
        self, current_phase: ConversationPhase, intent: Intent, has_pending_required_fields: bool
    ) -> ConversationPhase:
        phase_table = _ROUTING.get(current_phase, {})
        if intent in phase_table:
            return phase_table[intent]

        if current_phase == ConversationPhase.DISCOVERY and intent == Intent.AFFIRMATIVE:
            return (
                ConversationPhase.DISCOVERY
                if has_pending_required_fields
                else ConversationPhase.CLOSING
            )

        if current_phase == ConversationPhase.OBJECTION_HANDLING and intent == Intent.AFFIRMATIVE:
            return (
                ConversationPhase.DISCOVERY
                if has_pending_required_fields
                else ConversationPhase.CLOSING
            )

        # question/off_topic/unclear/objection-not-in-table: stay put
        return current_phase
