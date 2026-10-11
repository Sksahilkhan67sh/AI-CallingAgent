"""Request/response models for /api/v1/admin/data (CP15A)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErasureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number: str | None = Field(default=None, max_length=64)
    contact_id: uuid.UUID | None = None
    reason: str = Field(min_length=3, max_length=500)
    suppress: bool = True

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "ErasureRequest":
        if (self.phone_number is None) == (self.contact_id is None):
            raise ValueError("provide exactly one of phone_number or contact_id")
        return self


class ErasureResponse(BaseModel):
    already_erased: bool
    suppressed: bool
    last4: str | None
    counts: dict[str, int]


class LegalHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool
    reason: str = Field(min_length=3, max_length=500)


class LegalHoldResponse(BaseModel):
    active: bool
    changed: bool


class RetentionStatusResponse(BaseModel):
    last_run_id: uuid.UUID | None
    last_run_mode: str | None
    last_run_status: str | None
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_counts: dict | None
    # Rows currently past their retention period; None = that class is disabled.
    backlog: dict[str, int | None]
