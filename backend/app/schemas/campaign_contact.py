"""Schemas for campaign/contact association and bulk import results."""

import uuid

from pydantic import BaseModel


class CampaignContactResponse(BaseModel):
    """Result of associating (or reassigning) a contact to a campaign."""

    contact_id: uuid.UUID
    campaign_id: uuid.UUID
    status: str


class CampaignContactCounts(BaseModel):
    """Step 21 -- efficient aggregate counts, not per-contact detail."""

    total: int
    eligible: int
    suppressed: int


class BulkImportRowError(BaseModel):
    row: int
    reason: str


class BulkContactImportResult(BaseModel):
    campaign_id: uuid.UUID | None
    total: int
    created: int
    duplicates: int
    invalid: int
    errors: list[BulkImportRowError]
    # CP14: valid rows whose number is already on the do-not-call list. They are still
    # imported; the dial-time suppression check decides.
    suppressed_count: int = 0
