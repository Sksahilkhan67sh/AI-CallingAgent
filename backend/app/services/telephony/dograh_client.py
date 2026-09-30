"""HTTP client for Dograh's public API -- Checkpoints 08 and 09.

Dograh runs telephony, STT, LLM and TTS for a `calling_engine="dograh"`
call. This module is the only place that speaks HTTP to Dograh and the
only place that classifies Dograh failures; callers branch on
`DograhApiError.kind` / `.ambiguous`, never on raw exceptions.

Endpoints used (all verified against dograh-hq/dograh docs and OpenAPI):
  POST /api/v1/public/agent/{uuid}              production trigger
  POST /api/v1/public/agent/test/{uuid}         draft trigger (non-production only)
  GET  /api/v1/workflow/{workflow_id}/runs/{run_id}
  GET  /api/v1/workflow/{workflow_id}/runs      (dateRange filter, paginated)
"""

import enum
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

_MAX_LIST_PAGES = 5
_LIST_PAGE_SIZE = 100


class ProviderErrorKind(str, enum.Enum):
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTHENTICATION_ERROR = "authentication_error"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    AMBIGUOUS_REQUEST = "ambiguous_request"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


class DograhApiError(Exception):
    """`ambiguous=True` means the request may have reached Dograh and a run
    may exist: the caller must reconcile and must never blindly retry."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        kind: ProviderErrorKind | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(f"Dograh API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.kind = kind or _kind_for_status(status_code)
        self.ambiguous = ambiguous


class DograhConfigurationError(Exception):
    """A required setting is missing or unsafe -- never retried like a dial failure."""


@dataclass
class DograhTriggerResult:
    workflow_run_id: int
    workflow_run_name: str


@dataclass
class DograhRun:
    run_id: int
    is_completed: bool
    initial_context: dict[str, Any]
    gathered_context: dict[str, Any]
    cost_info: dict[str, Any]
    transcript_url: str | None
    recording_url: str | None


def _kind_for_status(status: int) -> ProviderErrorKind:
    if status in (401, 403):
        return ProviderErrorKind.AUTHENTICATION_ERROR
    if status == 422:
        return ProviderErrorKind.VALIDATION_ERROR
    if status == 429:
        return ProviderErrorKind.RATE_LIMITED
    if status in (400, 404):
        return ProviderErrorKind.PROVIDER_REJECTED
    if status == 408:
        return ProviderErrorKind.TIMEOUT
    if status >= 500:
        return ProviderErrorKind.PROVIDER_UNAVAILABLE
    if status == 0:
        return ProviderErrorKind.CONNECTION_ERROR
    return ProviderErrorKind.UNKNOWN_PROVIDER_ERROR


# Failures raised before any request bytes could have reached Dograh: a run
# cannot exist, so retrying through RecoveryManager is safe.
_NOT_SENT = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


class DograhClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        trigger_uuid: str,
        mode: str = "test",
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
        workflow_id: int | None = None,
    ) -> None:
        if not api_key or not trigger_uuid:
            raise DograhConfigurationError(
                "DOGRAH_API_KEY and DOGRAH_TRIGGER_UUID must both be set "
                "when CALLING_ENGINE=dograh"
            )
        if mode not in ("test", "production"):
            raise DograhConfigurationError("DOGRAH_TRIGGER_MODE must be 'test' or 'production'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.trigger_uuid = trigger_uuid
        self.mode = mode
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self.workflow_id = workflow_id

    def _trigger_path(self) -> str:
        if self.mode == "production":
            return f"/api/v1/public/agent/{self.trigger_uuid}"
        return f"/api/v1/public/agent/test/{self.trigger_uuid}"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-API-Key": self.api_key}

    def trigger_call(
        self, *, phone_number: str, initial_context: dict[str, Any]
    ) -> DograhTriggerResult:
        url = f"{self.base_url}{self._trigger_path()}"
        try:
            response = httpx.post(
                url,
                headers=self._headers(),
                json={"phone_number": phone_number, "initial_context": initial_context},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise DograhApiError(
                408,
                f"request timed out ({type(exc).__name__})",
                kind=ProviderErrorKind.TIMEOUT,
                ambiguous=not isinstance(exc, _NOT_SENT),
            ) from exc
        except httpx.HTTPError as exc:
            raise DograhApiError(
                0,
                f"request failed ({type(exc).__name__})",
                kind=ProviderErrorKind.CONNECTION_ERROR,
                ambiguous=not isinstance(exc, _NOT_SENT),
            ) from exc

        if response.status_code >= 400:
            # A 5xx may follow run creation inside Dograh: ambiguous.
            raise DograhApiError(
                response.status_code,
                _extract_detail(response),
                ambiguous=response.status_code >= 500,
            )

        try:
            body = response.json()
            return DograhTriggerResult(
                workflow_run_id=int(body["workflow_run_id"]),
                workflow_run_name=str(body.get("workflow_run_name", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DograhApiError(
                response.status_code,
                "2xx response without a usable workflow_run_id",
                kind=ProviderErrorKind.AMBIGUOUS_REQUEST,
                ambiguous=True,
            ) from exc

    # -- reconciliation reads -------------------------------------------

    def _require_workflow_id(self) -> int:
        if self.workflow_id is None:
            raise DograhConfigurationError("DOGRAH_WORKFLOW_ID is required for reconciliation")
        return self.workflow_id

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise DograhApiError(408, "request timed out", kind=ProviderErrorKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise DograhApiError(
                0, f"request failed ({type(exc).__name__})", kind=ProviderErrorKind.CONNECTION_ERROR
            ) from exc
        if response.status_code >= 400:
            raise DograhApiError(response.status_code, _extract_detail(response))
        try:
            return response.json()
        except ValueError as exc:
            raise DograhApiError(
                response.status_code,
                "non-JSON response",
                kind=ProviderErrorKind.UNKNOWN_PROVIDER_ERROR,
            ) from exc

    def get_run(self, run_id: int) -> DograhRun:
        wf = self._require_workflow_id()
        return _parse_run(self._get(f"/api/v1/workflow/{wf}/runs/{run_id}"))

    def find_run_by_attempt(self, *, call_attempt_id: str, since: datetime) -> DograhRun | None:
        """Newest-first scan of runs created since `since`, matching our
        correlation id in `initial_context`. Bounded to a few pages."""
        wf = self._require_workflow_id()
        filters = json.dumps(
            [{"attribute": "dateRange", "value": {"from": since.isoformat()}}]
        )
        for page in range(1, _MAX_LIST_PAGES + 1):
            body = self._get(
                f"/api/v1/workflow/{wf}/runs",
                {
                    "page": page,
                    "limit": _LIST_PAGE_SIZE,
                    "filters": filters,
                    "sort_by": "created_at",
                    "sort_order": "desc",
                },
            )
            for raw in body.get("runs", []):
                ctx = raw.get("initial_context") or {}
                if str(ctx.get("call_attempt_id")) == call_attempt_id:
                    return _parse_run(raw)
            if page >= int(body.get("total_pages", 1)):
                break
        return None


def _parse_run(raw: dict[str, Any]) -> DograhRun:
    return DograhRun(
        run_id=int(raw["id"]),
        is_completed=bool(raw.get("is_completed")),
        initial_context=raw.get("initial_context") or {},
        gathered_context=raw.get("gathered_context") or {},
        cost_info=raw.get("cost_info") or {},
        transcript_url=raw.get("transcript_url"),
        recording_url=raw.get("recording_url"),
    )


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or response.text)[:500]
    except ValueError:
        pass
    return response.text[:500]
