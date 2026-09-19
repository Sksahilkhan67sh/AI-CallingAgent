"""Telephony webhook payload -- Checkpoint 03 Step 41.

Only call-initiation/status fields; no conversation events (out of
scope this checkpoint).
"""

from pydantic import BaseModel


class TelephonyCallStatusWebhook(BaseModel):
    event_id: str
    provider_call_id: str
    status: str  # "connected" | "failed"
    failure_reason: str | None = None
