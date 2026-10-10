"""DograhQAAnalysisLLM -- CP14B.

Dograh is the approved LLM platform and exposes NO documented API to analyze an existing
transcript on demand. Its post-call analysis is the workflow's QA node, which runs
automatically after each eligible call and stores its JSON in the run's `annotations`
(https://docs.dograh.com/voice-agent/qa). This adapter therefore makes NO LLM request: it
READS that result through Dograh's documented GET run endpoint, validates it strictly, and
classifies every way that can go wrong so the worker can retry, skip or fail correctly.

  * never triggers analysis or calls an undocumented endpoint;
  * exactly ONE bounded HTTP request per analyze() call (no internal retry -- the worker is
    the sole retry owner);
  * never logs or raises transcript text, phone numbers, credentials or raw payloads.
"""

import logging
from typing import Any

from app.core.config import get_settings
from app.services.analysis.llm.base import (
    AnalysisContext,
    AnalysisLLM,
    AnalysisLLMPermanentError,
    AnalysisLLMProviderError,
    AnalysisLLMRateLimitError,
    AnalysisLLMTimeoutError,
    AnalysisLLMValidationError,
    AnalysisResultNotReady,
    AnalysisResultUnavailable,
)
from app.services.analysis.llm.qa_contract import find_qa_payload, parse_qa_payload
from app.services.analysis.llm.schemas import AnalysisResult
from app.services.telephony.dograh_client import DograhApiError, DograhClient, DograhErrorCategory

logger = logging.getLogger("analysis.dograh_qa")


class DograhQAAnalysisLLM(AnalysisLLM):
    #: provenance recorded on the CallAnalysis row
    provider_name = "dograh"
    model_name = "dograh-qa-node"

    def __init__(self, client: DograhClient) -> None:
        self._client = client
        # Dograh-reported run-level charge from the most recent analyze(); NOT attributed to
        # QA (UNVERIFIED whether it includes post-call QA tokens).
        self.last_observed_run_cost_usd: float | None = None

    def analyze(
        self,
        transcript_lines: list[str],
        *,
        brand_name: str,
        context: AnalysisContext | None = None,
    ) -> AnalysisResult:
        # transcript_lines is intentionally unused: the QA node already read the transcript
        # inside Dograh. It is never re-sent anywhere.
        if context is None or context.dograh_workflow_id is None or context.dograh_run_id is None:
            raise AnalysisLLMPermanentError("missing Dograh workflow/run identifiers")
        settings = get_settings()
        self.last_observed_run_cost_usd = None

        try:
            run = self._client.get_run(
                workflow_id=context.dograh_workflow_id, run_id=context.dograh_run_id
            )
        except DograhApiError as exc:
            raise _classify_api_error(exc) from None

        self.last_observed_run_cost_usd = _charge_usd(run)

        annotations = run.get("annotations")
        payload = find_qa_payload(annotations, configured_key=settings.dograh_qa_annotation_key)
        if payload is None:
            raise _no_result_yet_or_never(run, context)
        return parse_qa_payload(payload, max_bytes=settings.analysis_max_output_bytes)


def _no_result_yet_or_never(run: dict[str, Any], context: AnalysisContext) -> Exception:
    """Annotations absent. Documented facts: QA runs only after the call ends, skips calls
    under the minimum duration and voicemail, and may be sampled. Not documented: whether
    annotations exist when the webhook fires. So: run not completed => definitely not ready;
    completed but no QA entry => not ready until the worker's deadline passes (it then
    converts to SKIPPED), because "still running" and "never ran" are indistinguishable from
    the API (DOCUMENTED LIMITATION)."""
    if run.get("is_completed") is False:
        return AnalysisResultNotReady("run not completed")
    min_duration = get_settings().dograh_qa_min_duration_seconds
    if context.call_duration_seconds is not None and context.call_duration_seconds < min_duration:
        return AnalysisResultUnavailable("call shorter than the QA minimum duration")
    return AnalysisResultNotReady("QA annotations not present yet")


def _charge_usd(run: dict[str, Any]) -> float | None:
    cost_info = run.get("cost_info")
    if isinstance(cost_info, dict):
        for key in ("charge_usd", "total_cost_usd", "total_cost"):
            value = cost_info.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                return float(value)
    return None


def _classify_api_error(exc: DograhApiError) -> Exception:
    category = exc.category
    if category == DograhErrorCategory.RATE_LIMITED:
        return AnalysisLLMRateLimitError(
            "Dograh rate limited the request", retry_after_seconds=exc.retry_after_seconds
        )
    if category in (DograhErrorCategory.AUTHENTICATION_ERROR, DograhErrorCategory.VALIDATION_ERROR):
        return AnalysisLLMPermanentError(f"Dograh rejected the request ({exc.status_code})")
    if category == DograhErrorCategory.PROVIDER_REJECTED:
        # 404: unknown workflow/run -- a wrong identifier will not heal on retry.
        return AnalysisLLMPermanentError("Dograh run not found")
    if category in (DograhErrorCategory.TIMEOUT, DograhErrorCategory.AMBIGUOUS_REQUEST):
        # A GET is idempotent, so "response lost" is simply retriable here (unlike a trigger).
        return AnalysisLLMTimeoutError("Dograh request timed out")
    if category == DograhErrorCategory.UNKNOWN_PROVIDER_ERROR and 200 <= exc.status_code < 300:
        return AnalysisLLMValidationError("invalid QA output: unusable run response")
    return AnalysisLLMProviderError(f"Dograh unavailable ({category.value})")
