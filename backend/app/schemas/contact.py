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
