"""API schemas for the global do-not-call list (CP14). Numbers are never returned in full:
the dashboard convention (CP07 §13) is a masked number plus its last four digits."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SuppressionSource


class SuppressionAddRequest(BaseModel):
    phone_number: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=200)
    # Optional link to a contact. It must be the contact that owns this number.
    contact_id: uuid.UUID | None = None


class SuppressionRemoveRequest(BaseModel):
    # Mandatory: removing a number re-enables calling it, so the audit trail must say why.
    reason: str = Field(min_length=3, max_length=200)


class SuppressionResponse(BaseModel):
    id: uuid.UUID
    phone_masked: str
    last4: str
    source: SuppressionSource
    reason: str | None
    contact_id: uuid.UUID | None
    created_by: str | None
    requested_at: datetime


class SuppressionAddResponse(SuppressionResponse):
    # False: the number was already on the list and nothing changed (idempotent no-op).
    created: bool


class SuppressionImportRowError(BaseModel):
    row: int
    reason: str


class SuppressionImportResult(BaseModel):
    total: int
    added: int
    already_present: int
    invalid: int
    errors: list[SuppressionImportRowError]
