"""Pydantic API schemas for Campaign -- separate from the ORM model."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)


class CampaignUpdate(BaseModel):
    """Only fields legitimately mutable after creation. `status` changes
    go through CampaignService.transition_status (validated transitions),
    not a bare field assignment here."""

    name: str | None = Field(default=None, min_length=1)
    status: CampaignStatus | None = None


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: CampaignStatus
    created_at: datetime
