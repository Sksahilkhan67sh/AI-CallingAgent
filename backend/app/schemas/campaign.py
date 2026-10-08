"""Pydantic API schemas for Campaign -- separate from the ORM model."""

import uuid
from datetime import datetime

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import CampaignStatus
from app.services.calling_window import InvalidTimezoneError, load_timezone


def _valid_timezone(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        load_timezone(value)
    except InvalidTimezoneError as exc:
        raise ValueError("timezone must be a valid IANA timezone name") from exc
    return value


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)
    # CP14: both default from settings (Asia/Kolkata / IN) when omitted.
    timezone: str | None = None
    default_region: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return _valid_timezone(value)

    @field_validator("default_region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value is None:
            return value
        region = value.strip().upper()
        if region not in phonenumbers.SUPPORTED_REGIONS:
            raise ValueError("default_region must be a valid ISO 3166-1 alpha-2 country code")
        return region


class CampaignUpdate(BaseModel):
    """Only fields legitimately mutable after creation. `status` changes
    go through CampaignService.transition_status (validated transitions),
    not a bare field assignment here. `default_region` is deliberately NOT editable:
    existing contacts were parsed with it, and changing it would silently re-read their
    numbers as a different country's."""

    name: str | None = Field(default=None, min_length=1)
    status: CampaignStatus | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return _valid_timezone(value)


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: CampaignStatus
    timezone: str
    default_region: str
    created_at: datetime
