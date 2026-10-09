"""API schemas for a campaign's retry policy (CP14). Bounds that depend on settings
(max retries ceiling, backoff floor, the hard calling window) are enforced in
`app.services.retry_policy_service`, which reads them at request time."""

import uuid
from datetime import time

from pydantic import BaseModel, Field


class RetryPolicyUpdate(BaseModel):
    max_retries: int = Field(ge=0)
    retry_spacing_seconds: list[int]
    window_start: time
    window_end: time
    # Omitted -> unchanged. A campaign-level setting, edited here because the window is
    # meaningless without the zone it is read in.
    timezone: str | None = None
    never_connected_rules: dict[str, bool] | None = None
    mid_call_rules: dict[str, bool] | None = None


class RetryPolicyResponse(BaseModel):
    campaign_id: uuid.UUID
    # False: the campaign has no stored row yet and this is the DEFAULT policy in force.
    persisted: bool
    max_retries: int
    retry_spacing_seconds: list[int]
    window_start: time
    window_end: time
    timezone: str
    hard_window_start: time
    hard_window_end: time
    never_connected_rules: dict[str, bool]
    mid_call_rules: dict[str, bool]
