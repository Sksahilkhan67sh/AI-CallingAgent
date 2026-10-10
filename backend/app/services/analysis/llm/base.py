"""Analysis LLM abstraction -- Checkpoint 06 §12, extended by CP14B.

analyze(transcript_lines, brand_name, context) -> AnalysisResult. No provider-specific
response object ever leaves an implementation of this interface.

CP14B failure taxonomy. The WORKER owns retry policy (one retry layer, no nested loops);
an implementation only classifies what went wrong by raising exactly one of:

  AnalysisLLMTimeoutError / AnalysisLLMProviderError -> TRANSIENT (retry with backoff)
  AnalysisLLMRateLimitError                          -> RATE_LIMITED (honors retry_after)
  AnalysisLLMValidationError                         -> INVALID_OUTPUT (bounded retry)
  AnalysisLLMPermanentError                          -> PERMANENT (no retry: bad credentials,
                                                        unknown run, misconfiguration)
  AnalysisResultNotReady                             -> QA_NOT_READY (poll; no attempt consumed)
  AnalysisResultUnavailable                          -> QA_UNAVAILABLE (QA will never produce a
                                                        result for this run -> SKIPPED)

Exception MESSAGES must be safe to persist: never a transcript, phone number, credential or
raw provider payload.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.services.analysis.llm.schemas import AnalysisResult


class AnalysisLLMTimeoutError(Exception):
    pass


class AnalysisLLMProviderError(Exception):
    pass


class AnalysisLLMValidationError(Exception):
    """Raised when the provider's response doesn't satisfy the structured contract -- e.g.
    empty/malformed JSON, an unrecognized enum value. Never silently converted into a
    fabricated default (§20)."""


class AnalysisLLMRateLimitError(Exception):
    def __init__(self, message: str = "rate limited", *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AnalysisLLMPermanentError(Exception):
    """Will not succeed on retry without operator intervention (credentials, configuration,
    unknown run)."""


class AnalysisResultNotReady(Exception):
    """The provider has not produced the analysis YET (QA runs after the call ends)."""


class AnalysisResultUnavailable(Exception):
    """The provider will not produce an analysis for this run (QA disabled, skipped, sampled
    out, or it ran and recorded nothing usable)."""


@dataclass(frozen=True)
class AnalysisContext:
    """Provider-correlation identifiers for one analysis. Optional so existing callers and
    the mock provider keep the CP06 two-argument call shape."""

    dograh_workflow_id: int | None = None
    dograh_run_id: int | None = None
    call_duration_seconds: float | None = None


class AnalysisLLM(ABC):
    @abstractmethod
    def analyze(
        self,
        transcript_lines: list[str],
        *,
        brand_name: str,
        context: AnalysisContext | None = None,
    ) -> AnalysisResult:
        """Raises one of the exceptions above on failure -- the worker owns retry /
        bounded-attempt policy (§20-21), not this interface."""
