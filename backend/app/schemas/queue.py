"""Schemas for queue admission/enqueue results."""

import uuid

from pydantic import BaseModel


class EnqueueResult(BaseModel):
    campaign_id: uuid.UUID
    enqueued: int
    skipped_suppressed: int
    skipped_duplicate: int  # already queued (guard present)
    # CP12-C -- additive, defaulted so existing clients keep working. Invariant for a
    # finished run: discovered == enqueued + skipped_suppressed + skipped_duplicate.
    discovered: int = 0
    pages_processed: int = 0
    # False only when the per-request cap stopped the run with eligible contacts left:
    # call enqueue again (idempotent) to continue.
    complete: bool = True
