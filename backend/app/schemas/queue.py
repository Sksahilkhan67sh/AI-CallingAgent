"""Schemas for queue admission/enqueue results."""

import uuid

from pydantic import BaseModel


class EnqueueResult(BaseModel):
    campaign_id: uuid.UUID
    enqueued: int
    skipped_suppressed: int
    skipped_duplicate: int
