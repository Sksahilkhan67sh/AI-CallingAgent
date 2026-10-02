"""Schema for Dograh's post-call webhook -- Checkpoint 08, hardened in
Checkpoint 09 §3.2.

This payload shape is NOT fixed by Dograh itself -- Dograh's Webhook
node sends whatever `payload_template` you configure in the workflow,
rendered from the variables documented in Dograh's own webhook
developer reference (workflow_run_id, initial_context, gathered_context,
cost_info, recording_url, transcript_url, ...). The exact template this
schema expects is documented in docs/CHECKPOINT-08-NOTES.md and must be
pasted into the Dograh workflow's Webhook node verbatim.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# §3.2: bound every free-text field's length -- a webhook endpoint is
# an unauthenticated-until-checked attack surface; nothing here should
# accept an arbitrarily large body before authentication even runs
# (authentication happens at the route layer, before this schema is
# even parsed against a real body -- see app/api/routes/dograh_webhook.py
# -- but the model itself must not trust the caller either).
_MAX_STATUS_LEN = 500
_MAX_URL_LEN = 2048


class DograhWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate Dograh adding template variables later

    call_attempt_id: str = Field(min_length=1, max_length=64)
    workflow_run_id: int | str | None = None
    call_status: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    call_disposition: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    mapped_call_disposition: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    duration_seconds: float | str | None = None
    recording_url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    transcript_url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    call_time: str | None = Field(default=None, max_length=128)

    @field_validator("recording_url", "transcript_url")
    @classmethod
    def _must_be_http_url_if_present(cls, value: str | None) -> str | None:
        if value is not None and not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("must be an http(s) URL")
        return value
