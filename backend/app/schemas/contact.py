"""Pydantic API schemas for Contact -- separate from the ORM model."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import ContactStatus
from app.services.phone import InvalidPhoneNumberError, normalize_phone_number


class ContactCreate(BaseModel):
    campaign_id: uuid.UUID
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def phone_number_must_be_valid(cls, value: str) -> str:
        try:
            normalize_phone_number(value)
        except InvalidPhoneNumberError as exc:
            raise ValueError(str(exc)) from exc
        return value


class ContactUpdate(BaseModel):
    """Only phone_number is mutable here. `campaign_id` changes go
    through the dedicated campaign-association endpoint (eligibility/
    suppression checks apply); `status` changes go through the
    dedicated deactivation endpoint. Internal fields (id, attempt_count,
    timestamps) are never client-settable."""

    phone_number: str | None = None

    @field_validator("phone_number")
    @classmethod
    def phone_number_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            normalize_phone_number(value)
        except InvalidPhoneNumberError as exc:
            raise ValueError(str(exc)) from exc
        return value


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
