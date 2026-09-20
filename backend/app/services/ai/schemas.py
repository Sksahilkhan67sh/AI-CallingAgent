"""LLM structured output -- Prompt-Specification.md §4, verbatim.

This is the one shape every LLM provider adapter must produce,
regardless of how that provider's own API represents it -- the rest of
the orchestration code (policy, memory, transcript) only ever sees this
type, never a provider-specific response object (Checkpoint 04 Step 19).
"""

from dataclasses import dataclass

from app.models.enums import Intent, NextAction, RecordingConsent

# Prompt-Specification.md §4 note: absent entity fields stay
# "not_captured" rather than being guessed.
NOT_CAPTURED = "not_captured"


@dataclass
class Entities:
    interest_level: str = NOT_CAPTURED  # interested | not_interested | undecided | not_captured
    objection: str | None = None
    follow_up_preference: str | None = None
    decision_maker_status: str = NOT_CAPTURED  # sole | influencer | not_involved | not_captured
    free_text_feedback: str | None = None


@dataclass
class StructuredOutput:
    intent: Intent
    entities: Entities
    next_action: NextAction
    response_text: str
    requires_suppression: bool = False
    recording_consent: RecordingConsent = RecordingConsent.NOT_APPLICABLE


@dataclass
class TurnContext:
    """What the LLM abstraction is given to produce a StructuredOutput --
    Checkpoint 04 Step 19's generate_response(system_context, memory,
    recent_messages, user_utterance), bundled as one type so the
    interface stays small."""

    system_context: str
    recent_messages: list[str]
    user_utterance: str
    is_reconnect: bool = False
    last_agent_utterance: str | None = None
