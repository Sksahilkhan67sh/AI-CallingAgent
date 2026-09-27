"""Schema for Dograh's post-call webhook -- Checkpoint 08.

This payload shape is NOT fixed by Dograh itself -- Dograh's Webhook
node sends whatever `payload_template` you configure in the workflow,
rendered from the variables documented in Dograh's own webhook
developer reference (workflow_run_id, initial_context, gathered_context,
cost_info, recording_url, transcript_url, ...). The exact template this
schema expects is documented in docs/CHECKPOINT-08-NOTES.md and must be
pasted into the Dograh workflow's Webhook node verbatim.
"""

from pydantic import BaseModel


class DograhWebhookPayload(BaseModel):
    call_attempt_id: str
    workflow_run_id: int | str | None = None
    call_status: str | None = None
    call_disposition: str | None = None
    mapped_call_disposition: str | None = None
    duration_seconds: float | str | None = None
    recording_url: str | None = None
    transcript_url: str | None = None
    call_time: str | None = None
