"""Thin HTTP client for Dograh's public API Trigger endpoint --
Checkpoint 08, hardened in Checkpoint 09. Dograh is a separate,
self-hosted (or Dograh Cloud) voice-agent platform: telephony, STT,
LLM, and TTS for a `calling_engine="dograh"` call all happen inside
Dograh's own pipeline, not this codebase. This client only ever calls
the one endpoint Dograh exposes for "start a call from an external
backend" -- see docs/CHECKPOINT-08-NOTES.md and
docs/CHECKPOINT-09-NOTES.md for the full contract this was built
against (Dograh's own `API Trigger` node reference).
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


class DograhErrorCategory(str, enum.Enum):
    """Checkpoint 09 §1.4 -- a small, explicit taxonomy so callers
    (the dialer worker, RecoveryManager) branch on a stable category
    instead of parsing HTTP status codes or exception types themselves.
    Kept entirely inside this module -- provider-specific handling
    never leaks into business logic (§1.4)."""

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTHENTICATION_ERROR = "authentication_error"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    AMBIGUOUS_REQUEST = "ambiguous_request"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


# §1.3: categories where the outbound request may have reached Dograh
# before we lost the response -- these must never be treated as a
# confirmed failure and blindly retried. AMBIGUOUS_REQUEST covers a
# read/write timeout explicitly; TIMEOUT (generic, unclassified) is
# included too because failing *safe* toward "might have happened" is
# the conservative choice when we can't prove otherwise.
AMBIGUOUS_CATEGORIES = frozenset(
    {DograhErrorCategory.AMBIGUOUS_REQUEST, DograhErrorCategory.TIMEOUT}
)

_RECONCILE_PAGE_SIZE = 100  # Dograh's documented maximum
_RECONCILE_MAX_PAGES = 5
_RECONCILE_WINDOW_PADDING = timedelta(seconds=60)  # clock skew between us and Dograh


class DograhApiError(Exception):
    def __init__(
        self, status_code: int, message: str, *, category: DograhErrorCategory
    ) -> None:
        super().__init__(f"Dograh API error {status_code} [{category.value}]: {message}")
        self.status_code = status_code
        self.message = message
        self.category = category

    @property
    def is_ambiguous(self) -> bool:
        return self.category in AMBIGUOUS_CATEGORIES


class DograhConfigurationError(Exception):
    """Raised when calling_engine="dograh" but a required setting
    (api key, trigger UUID) is missing -- a configuration problem, not
    a call-time failure, so it's never retried like a dial failure."""


@dataclass
class DograhTriggerResult:
    workflow_run_id: int
    workflow_run_name: str


def _classify_status_code(status_code: int) -> DograhErrorCategory:
    if status_code in (401, 403):
        return DograhErrorCategory.AUTHENTICATION_ERROR
    if status_code == 429:
        return DograhErrorCategory.RATE_LIMITED
    if status_code == 404:
        # Trigger UUID not found -- a configuration problem Dograh
        # validly rejected, not ambiguous and not a transient failure.
        return DograhErrorCategory.PROVIDER_REJECTED
    if status_code >= 500:
        return DograhErrorCategory.PROVIDER_UNAVAILABLE
    if 400 <= status_code < 500:
        return DograhErrorCategory.VALIDATION_ERROR
    return DograhErrorCategory.UNKNOWN_PROVIDER_ERROR


class DograhClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        trigger_uuid: str,
        mode: str = "test",
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
    ) -> None:
        if not api_key or not trigger_uuid:
            raise DograhConfigurationError(
                "DOGRAH_API_KEY and DOGRAH_TRIGGER_UUID must both be set "
                "when CALLING_ENGINE=dograh"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.trigger_uuid = trigger_uuid
        self.mode = mode
        # §1.2: bounded, separately-configurable connect vs. read
        # timeouts -- never a single unbounded call. httpx.Timeout's
        # `write`/`pool` phases are pinned to the connect timeout too,
        # since a slow write or an exhausted connection pool is the
        # same "give up fast" case as a slow connect, not the
        # slow-response case `read` covers.
        self.timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=connect_timeout, pool=connect_timeout
        )

    def _trigger_path(self) -> str:
        # Dograh's own test-vs-production distinction (its docs):
        # "test" always runs the workflow's latest draft; "production"
        # runs the published version only. §1.1: production must never
        # silently fall back to /test/ -- this is the only branch point.
        if self.mode == "production":
            return f"/api/v1/public/agent/{self.trigger_uuid}"
        return f"/api/v1/public/agent/test/{self.trigger_uuid}"

    def _send(self, send: Callable[..., httpx.Response], url: str, **kwargs: Any) -> httpx.Response:
        """One place that turns httpx failures and HTTP error statuses into
        the DograhApiError taxonomy, shared by the trigger and the
        reconciliation lookup."""
        try:
            response = send(url, timeout=self.timeout, **kwargs)
        except httpx.ConnectTimeout as exc:
            # Never even established a connection -- the request
            # definitely never reached Dograh. Not ambiguous.
            raise DograhApiError(
                0, f"connect timed out: {exc}", category=DograhErrorCategory.CONNECTION_ERROR
            ) from exc
        except httpx.PoolTimeout as exc:
            raise DograhApiError(
                0,
                f"connection pool exhausted: {exc}",
                category=DograhErrorCategory.CONNECTION_ERROR,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            # §1.3: the request may have been sent (and even processed)
            # before we lost the response -- this is the genuinely
            # ambiguous case. Caller must not blindly retry.
            raise DograhApiError(
                0, f"timed out waiting for response: {exc}",
                category=DograhErrorCategory.AMBIGUOUS_REQUEST,
            ) from exc
        except httpx.TimeoutException as exc:
            # Any other/future timeout subtype -- fail safe toward
            # ambiguous rather than assume it's a clean non-event.
            raise DograhApiError(
                0, f"request timed out: {exc}", category=DograhErrorCategory.TIMEOUT
            ) from exc
        except httpx.ConnectError as exc:
            raise DograhApiError(
                0, f"connection failed: {exc}", category=DograhErrorCategory.CONNECTION_ERROR
            ) from exc
        except httpx.HTTPError as exc:
            raise DograhApiError(
                0, f"request failed: {exc}", category=DograhErrorCategory.UNKNOWN_PROVIDER_ERROR
            ) from exc

        if response.status_code >= 400:
            detail = _extract_detail(response)
            raise DograhApiError(
                response.status_code, detail, category=_classify_status_code(response.status_code)
            )

        return response

    def trigger_call(
        self, *, phone_number: str, initial_context: dict[str, Any]
    ) -> DograhTriggerResult:
        response = self._send(
            httpx.post,
            f"{self.base_url}{self._trigger_path()}",
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
            json={"phone_number": phone_number, "initial_context": initial_context},
        )
        body = response.json()
        return DograhTriggerResult(
            workflow_run_id=body["workflow_run_id"],
            workflow_run_name=body.get("workflow_run_name", ""),
        )

    def find_runs_for_attempt(self, call_attempt_id: str, since: datetime) -> list[int]:
        """Reconciliation for an ambiguous trigger: which Dograh runs, if any,
        were created for this CallAttempt?

        Uses Dograh's documented org-wide run listing
        (GET /api/v1/organizations/usage/runs: `start_date`/`end_date` bound
        `created_at`, `limit` <= 100, each run carries its `initial_context`).
        Dograh offers no server-side filter on an arbitrary initial_context
        key, so matching on the `call_attempt_id` we already round-trip is done
        here, over a bounded time window. Verified against Dograh's published
        OpenAPI only -- NOT against a live instance.

        Raises DograhApiError when the answer cannot be trusted (provider
        error, malformed body, or a window larger than the page bound): "no
        match" is only ever returned when the whole window was actually read.
        """
        url = f"{self.base_url}/api/v1/organizations/usage/runs"
        window_start = (since - _RECONCILE_WINDOW_PADDING).astimezone(UTC).isoformat()
        window_end = datetime.now(UTC).isoformat()
        matches: set[int] = set()

        for page in range(1, _RECONCILE_MAX_PAGES + 1):
            response = self._send(
                httpx.get,
                url,
                headers={"X-API-Key": self.api_key},
                params={
                    "start_date": window_start,
                    "end_date": window_end,
                    "page": page,
                    "limit": _RECONCILE_PAGE_SIZE,
                },
            )
            try:
                body = response.json()
                runs = body["runs"]
                total_pages = int(body["total_pages"])
                for run in runs:
                    context = run.get("initial_context") or {}
                    if context.get("call_attempt_id") == call_attempt_id:
                        matches.add(int(run["id"]))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise DograhApiError(
                    response.status_code,
                    "unexpected run-list response shape",
                    category=DograhErrorCategory.UNKNOWN_PROVIDER_ERROR,
                ) from exc
            if page >= total_pages:
                return sorted(matches)

        raise DograhApiError(
            0,
            "reconciliation window exceeds the page bound; result would be incomplete",
            category=DograhErrorCategory.UNKNOWN_PROVIDER_ERROR,
        )


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or response.text)
    except ValueError:
        pass
    return response.text
