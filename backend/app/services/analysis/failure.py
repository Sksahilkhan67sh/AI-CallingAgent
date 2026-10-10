"""CP14B failure classification + retry timing for post-call analysis.

ONE retry owner: the analysis worker. Provider adapters make a single request per call and
only classify; there is no SDK/service/worker retry stacking, so the maximum number of
provider requests for one analysis is exactly `analysis_max_attempts` (plus bounded polling
for "QA not ready", which consumes no attempt and ends at a deadline).

`error_code` values persisted on CallAnalysis come ONLY from the closed set below -- never a
raw exception string, transcript fragment, URL or credential.
"""

import enum
import random
from collections.abc import Callable

from app.services.analysis.llm.base import (
    AnalysisLLMPermanentError,
    AnalysisLLMProviderError,
    AnalysisLLMRateLimitError,
    AnalysisLLMTimeoutError,
    AnalysisLLMValidationError,
    AnalysisResultNotReady,
    AnalysisResultUnavailable,
)


class FailureKind(str, enum.Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    INVALID_OUTPUT = "invalid_output"
    PERMANENT = "permanent"
    QA_NOT_READY = "qa_not_ready"
    QA_UNAVAILABLE = "qa_unavailable"


# Closed set of sanitized codes.
ERR_PROVIDER_TIMEOUT = "provider_timeout"
ERR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERR_RATE_LIMITED = "provider_rate_limited"
ERR_INVALID_OUTPUT = "invalid_output"
ERR_PERMANENT = "provider_permanent_error"
ERR_QA_NOT_READY = "qa_not_ready"
ERR_QA_UNAVAILABLE = "qa_unavailable"
ERR_QA_NOT_PRODUCED = "qa_result_not_produced"
ERR_NO_SESSION = "no_conversation_session"
ERR_EMPTY_CONVERSATION = "empty_conversation"
ERR_NOT_ELIGIBLE = "not_eligible"
ERR_BAD_RELATIONSHIP = "invalid_call_relationship"
ERR_MISSING_RUN_ID = "missing_provider_run_id"
ERR_LEASE_EXPIRED = "lease_expired_attempts_exhausted"
ERR_BUDGET_NOT_CONFIGURED = "budget_not_configured"
ERR_BUDGET_CAP_REACHED = "budget_cap_reached"


def classify(exc: Exception) -> tuple[FailureKind, str, int | None]:
    """(kind, sanitized error_code, retry_after_seconds). Anything unrecognised is treated
    as TRANSIENT-but-bounded by the attempt cap -- never silently as success."""
    if isinstance(exc, AnalysisResultNotReady):
        return FailureKind.QA_NOT_READY, ERR_QA_NOT_READY, None
    if isinstance(exc, AnalysisResultUnavailable):
        return FailureKind.QA_UNAVAILABLE, ERR_QA_UNAVAILABLE, None
    if isinstance(exc, AnalysisLLMRateLimitError):
        return FailureKind.RATE_LIMITED, ERR_RATE_LIMITED, exc.retry_after_seconds
    if isinstance(exc, AnalysisLLMPermanentError):
        return FailureKind.PERMANENT, ERR_PERMANENT, None
    if isinstance(exc, AnalysisLLMValidationError):
        return FailureKind.INVALID_OUTPUT, ERR_INVALID_OUTPUT, None
    if isinstance(exc, AnalysisLLMTimeoutError):
        return FailureKind.TRANSIENT, ERR_PROVIDER_TIMEOUT, None
    if isinstance(exc, AnalysisLLMProviderError):
        return FailureKind.TRANSIENT, ERR_PROVIDER_UNAVAILABLE, None
    return FailureKind.TRANSIENT, ERR_PROVIDER_UNAVAILABLE, None


def backoff_seconds(
    attempt_count: int,
    *,
    base: int,
    cap: int,
    retry_after: int | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with jitter: min(cap, base * 2**(attempt-1)) * (0.5 + 0.5*rng()).
    A provider Retry-After is honoured (clamped to `cap`) when it asks for longer."""
    exponent = max(attempt_count - 1, 0)
    delay = min(cap, base * (2**exponent)) * (0.5 + 0.5 * rng())
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(retry_after, cap))
    return float(delay)


def poll_delay_seconds(
    elapsed_seconds: float, *, base: int, cap: int, rng: Callable[[], float] = random.random
) -> float:
    """Progressive polling for "QA not ready": waits grow with the time already spent
    waiting (~half of it), so a 30-minute deadline costs roughly a dozen requests, not
    hundreds."""
    delay = min(cap, max(base, elapsed_seconds * 0.5)) * (0.75 + 0.25 * rng())
    return float(delay)
