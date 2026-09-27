"""Thin HTTP client for Dograh's public API Trigger endpoint --
Checkpoint 08. Dograh is a separate, self-hosted (or Dograh Cloud)
voice-agent platform: telephony, STT, LLM, and TTS for a
`calling_engine="dograh"` call all happen inside Dograh's own
pipeline, not this codebase. This client only ever calls the one
endpoint Dograh exposes for "start a call from an external backend" --
see docs/CHECKPOINT-08-NOTES.md for the full contract this was built
against (Dograh's own `API Trigger` node reference).
"""

from dataclasses import dataclass
from typing import Any

import httpx


class DograhApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Dograh API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class DograhConfigurationError(Exception):
    """Raised when calling_engine="dograh" but a required setting
    (api key, trigger UUID) is missing -- a configuration problem, not
    a call-time failure, so it's never retried like a dial failure."""


@dataclass
class DograhTriggerResult:
    workflow_run_id: int
    workflow_run_name: str


class DograhClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        trigger_uuid: str,
        mode: str = "test",
        timeout: float = 15.0,
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
        self.timeout = timeout

    def _trigger_path(self) -> str:
        # Dograh's own test-vs-production distinction (its docs):
        # "test" always runs the workflow's latest draft; "production"
        # runs the published version only.
        if self.mode == "production":
            return f"/api/v1/public/agent/{self.trigger_uuid}"
        return f"/api/v1/public/agent/test/{self.trigger_uuid}"

    def trigger_call(
        self, *, phone_number: str, initial_context: dict[str, Any]
    ) -> DograhTriggerResult:
        url = f"{self.base_url}{self._trigger_path()}"
        try:
            response = httpx.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                json={"phone_number": phone_number, "initial_context": initial_context},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise DograhApiError(408, f"request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DograhApiError(0, f"request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = _extract_detail(response)
            raise DograhApiError(response.status_code, detail)

        body = response.json()
        return DograhTriggerResult(
            workflow_run_id=body["workflow_run_id"],
            workflow_run_name=body.get("workflow_run_name", ""),
        )


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or response.text)
    except ValueError:
        pass
    return response.text
