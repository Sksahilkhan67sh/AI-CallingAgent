"""Analysis LLM abstraction -- Checkpoint 06 §12, mirroring
app/services/ai/llm/base.py's LLM interface for the conversation engine.

analyze(transcript_lines, context) -> AnalysisResult. No provider-specific
response object ever leaves an implementation of this interface.
"""

from abc import ABC, abstractmethod

from app.services.analysis.llm.schemas import AnalysisResult


class AnalysisLLMTimeoutError(Exception):
    pass


class AnalysisLLMProviderError(Exception):
    pass


class AnalysisLLMValidationError(Exception):
    """Raised by a provider adapter (or the worker's own post-validation)
    when the model's response doesn't satisfy the structured contract --
    e.g. empty/malformed JSON, an unrecognized enum value. Never
    silently converted into a fabricated default (§20)."""


class AnalysisLLM(ABC):
    @abstractmethod
    def analyze(self, transcript_lines: list[str], *, brand_name: str) -> AnalysisResult:
        """Raises AnalysisLLMTimeoutError / AnalysisLLMProviderError /
        AnalysisLLMValidationError on failure -- the caller (the
        analysis worker) owns retry/bounded-attempt policy (§20-21), not
        this interface."""
