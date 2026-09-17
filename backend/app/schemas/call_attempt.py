"""Pydantic API schema for CallAttempt (read-only in this checkpoint --
attempts are created by the future dialing worker, not via a public
create endpoint)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import (
    CallAttemptState,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
    RecordingConsent,
)


class CallAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    attempt_number: int
    state: CallAttemptState
    connection_failure_reason: NeverConnectedFailureReason | None
    disconnect_reason: MidCallDisconnectReason | None
    recording_consent: RecordingConsent | None
    started_at: datetime
    ended_at: datetime | None
