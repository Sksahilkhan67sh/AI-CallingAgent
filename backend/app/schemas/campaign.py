"""Pydantic API schemas for Campaign -- separate from the ORM model."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: CampaignStatus
    created_at: datetime
