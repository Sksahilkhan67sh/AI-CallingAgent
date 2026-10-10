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
    # CP10: optional echoes of the initial_context we send. When present they
    # must match the attempt's own contact/campaign (see
    # dograh_webhook_service._validate_correlation); absent => not checked, so
    # a workflow still on the CP08 payload_template keeps working.
    contact_id: str | None = Field(default=None, max_length=64)
    campaign_id: str | None = Field(default=None, max_length=64)
    workflow_run_id: int | str | None = None
    # CP14B: the Dograh workflow (agent) id, needed to GET the run's QA annotations. Optional so
    # templates that predate it keep validating; DOGRAH_WORKFLOW_ID is the fallback.
    workflow_id: int | str | None = None

    @field_validator("workflow_id", mode="before")
    @classmethod
    def _workflow_id_is_not_a_boolean(cls, value: object) -> object:
        # pydantic would coerce JSON `true` to 1; a boolean is never a valid workflow id.
        return None if isinstance(value, bool) else value

    call_status: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    call_disposition: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    mapped_call_disposition: str | None = Field(default=None, max_length=_MAX_STATUS_LEN)
    duration_seconds: float | str | None = None
    recording_url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    transcript_url: str | None = Field(default=None, max_length=_MAX_URL_LEN)
    call_time: str | None = Field(default=None, max_length=128)

    @field_validator("recording_url", mode="before")
    @classmethod
    def _recording_url_is_advisory(cls, value: object) -> str | None:
        """CP15 prep: `recording_url` is advisory and must never reject a webhook.

        Dograh renders `{{recording_url}}` as a public download URL when the run has a public
        token, but as the bare storage key (e.g. ``recordings/123.wav``) when it has none (Dograh
        source, api/tasks/run_integrations.py). A bare key, a non-string, an over-long value or
        anything that is not a clean http(s) URL is therefore normalized to ``None`` ("no
        downloadable recording") instead of failing validation and losing the whole call result.

        This value is NOT trusted, NOT fetched, and NOT persisted by anything today (no consumer
        exists). Any future downloader must re-fetch the run via the authenticated `get_run`
        and apply its own host allow-list/SSRF policy; it must never use this string as-is. A
        Dograh public-download URL embeds a permanent bearer token, so it must not be logged.
        """
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate or len(candidate) > _MAX_URL_LEN:
            return None
        if not (candidate.startswith("http://") or candidate.startswith("https://")):
            return None
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            return None
        return candidate

    @field_validator("transcript_url")
    @classmethod
    def _must_be_http_url_if_present(cls, value: str | None) -> str | None:
        # Unchanged: the transcript URL IS fetched (transcript_fetch.py, SSRF-guarded), so it stays
        # strictly validated here as the first line of defence.
        if value is not None and not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("must be an http(s) URL")
        return value
