"""Schema for Dograh's post-call webhook -- Checkpoints 08, 09.

The payload shape is fixed by the `payload_template` configured in the
Dograh workflow's Webhook node (docs/CHECKPOINT-08-NOTES.md). Everything
here is untrusted input: types, lengths and URL schemes are enforced so
arbitrary JSON can never reach the database layer.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_Short = Annotated[str, Field(max_length=256)]
_Url = Annotated[str, Field(max_length=2048)]


def _tolerant_float(value: Any) -> float | None:
    """Dograh renders every template value as a string and renders
    missing values as '' / 'None' / 'null'; those mean 'unknown'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "nan"):
        return None
    return float(text)  # ValueError -> 422


class DograhWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    call_attempt_id: uuid.UUID
    workflow_run_id: int | Annotated[str, Field(max_length=64)] | None = None
    call_status: _Short | None = None
    call_disposition: _Short | None = None
    mapped_call_disposition: _Short | None = None
    duration_seconds: float | None = Field(default=None, ge=0, le=86_400)
    recording_url: _Url | None = None
    transcript_url: _Url | None = None
    call_time: datetime | None = None

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def _duration(cls, v: Any) -> float | None:
        return _tolerant_float(v)

    @field_validator("workflow_run_id", mode="before")
    @classmethod
    def _run_id(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in ("", "none", "null"):
            return None
        return v

    @field_validator("call_time", mode="before")
    @classmethod
    def _call_time(cls, v: Any) -> Any:
        return None if isinstance(v, str) and v.strip().lower() in ("", "none", "null") else v

    @field_validator("recording_url", "transcript_url", mode="before")
    @classmethod
    def _urls(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "null")):
            return None
        if not isinstance(v, str) or not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must be http(s)")
        return v

    def run_id_str(self) -> str | None:
        return None if self.workflow_run_id is None else str(self.workflow_run_id)
