"""LLM abstraction -- Checkpoint 04 Step 19.

generate_response(context) -> StructuredOutput. No provider-specific
response object ever leaves an implementation of this interface.
"""

from abc import ABC, abstractmethod

from app.services.ai.schemas import StructuredOutput, TurnContext


class LLMTimeoutError(Exception):
    pass


class LLMProviderError(Exception):
    pass


class LLM(ABC):
    @abstractmethod
    def generate_response(self, context: TurnContext) -> StructuredOutput:
        """Raises LLMTimeoutError / LLMProviderError on failure -- the
        caller (ConversationOrchestrator) owns retry/fallback policy
        (Step 20-21), not this interface."""
