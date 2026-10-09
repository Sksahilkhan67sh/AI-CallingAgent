"""Pydantic API schemas for Contact -- separate from the ORM model."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ContactStatus

# The number is parsed against the CAMPAIGN's region by ContactService (CP14): the schema can
# only bound the raw input, it cannot know which region the number belongs to.
_PHONE_FIELD = Field(min_length=1, max_length=64)


class ContactCreate(BaseModel):
    campaign_id: uuid.UUID
    phone_number: str = _PHONE_FIELD


class ContactUpdate(BaseModel):
    """Only phone_number is mutable here. `campaign_id` changes go
    through the dedicated campaign-association endpoint (eligibility/
    suppression checks apply); `status` changes go through the
    dedicated deactivation endpoint. Internal fields (id, attempt_count,
    timestamps) are never client-settable."""

    phone_number: str | None = Field(default=None, min_length=1, max_length=64)


class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    phone_number: str
    normalized_phone_number: str
    status: ContactStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime
