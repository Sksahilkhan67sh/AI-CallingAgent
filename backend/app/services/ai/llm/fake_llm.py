"""Deterministic fake LLM -- no real model is named in the specs and no
credentials exist in this environment (see docs/CHECKPOINT-04-NOTES.md).

Keyword-driven so tests can construct predictable conversations without
depending on an internet API: matches a handful of phrases from
Prompt-Specification.md's own guardrail language (opt-out phrases,
"speak to a human", decline words) to the corresponding intent, and
otherwise returns a generic in-script response. Real classification
accuracy is a real-provider concern; this fake exists to test
orchestration logic deterministically, not to imitate an actual model.
"""

from app.models.enums import Intent, NextAction
from app.services.ai.llm.base import LLM, LLMProviderError, LLMTimeoutError
from app.services.ai.schemas import Entities, StructuredOutput, TurnContext

_OPT_OUT_PHRASES = ("stop calling", "don't call", "do not call", "remove me", "no more calls")
_HUMAN_PHRASES = ("speak to a human", "talk to a person", "real person")
_DECLINE_PHRASES = ("not interested", "no thanks", "no thank you")
_AFFIRMATIVE_PHRASES = ("yes", "sure", "sounds good", "interested", "okay")


class FakeLLM(LLM):
    def __init__(self) -> None:
        self.calls: list[TurnContext] = []
        self.force_timeout = False
        self.force_error = False
        self.force_malformed = False

    def generate_response(self, context: TurnContext) -> StructuredOutput:
        self.calls.append(context)

        if self.force_timeout:
            raise LLMTimeoutError("fake LLM configured to time out")
        if self.force_error:
            raise LLMProviderError("fake LLM configured to error")
        if self.force_malformed:
            # Simulates a response that fails schema validation -- the
            # orchestrator's response validator must catch this itself;
            # this fake still returns *a* StructuredOutput (Python's
            # type system won't let this class return garbage), so
            # "malformed" is represented as an empty response_text,
            # which the validator (Step 18) rejects.
            return StructuredOutput(
                intent=Intent.UNCLEAR,
                entities=Entities(),
                next_action=NextAction.END_CALL_POLITE,
                response_text="",
            )

        utterance = context.user_utterance.lower()

        if context.is_reconnect:
            return StructuredOutput(
                intent=Intent.UNCLEAR,
                entities=Entities(),
                next_action=NextAction.CONTINUE_SCRIPT,
                response_text=(
                    f"Sorry about that, we got disconnected -- you were just telling me "
                    f"about {context.last_agent_utterance or 'where we left off'}. "
                    f"Would you like to pick up from there?"
                ),
            )

        if any(p in utterance for p in _OPT_OUT_PHRASES):
            return StructuredOutput(
                intent=Intent.END_CALL,
                entities=Entities(),
                next_action=NextAction.END_CALL_POLITE,
                requires_suppression=True,
                response_text="Understood, I'll make sure you're not contacted again. Take care.",
            )

        if any(p in utterance for p in _HUMAN_PHRASES):
            return StructuredOutput(
                intent=Intent.REQUEST_HUMAN,
                entities=Entities(),
                next_action=NextAction.ESCALATE_TO_HUMAN_OFFER,
                response_text="Of course -- I can have someone follow up with you directly.",
            )

        if any(p in utterance for p in _DECLINE_PHRASES):
            return StructuredOutput(
                intent=Intent.NEGATIVE,
                entities=Entities(interest_level="not_interested"),
                next_action=NextAction.END_CALL_POLITE,
                response_text="No problem at all, thanks for your time. Have a great day!",
            )

        if any(p in utterance for p in _AFFIRMATIVE_PHRASES):
            return StructuredOutput(
                intent=Intent.AFFIRMATIVE,
                entities=Entities(interest_level="interested"),
                next_action=NextAction.CONTINUE_SCRIPT,
                response_text="Great! Can you tell me a bit about your current setup?",
            )

        return StructuredOutput(
            intent=Intent.UNCLEAR,
            entities=Entities(),
            next_action=NextAction.CONTINUE_SCRIPT,
            response_text="Sorry, could you say that again?",
        )
