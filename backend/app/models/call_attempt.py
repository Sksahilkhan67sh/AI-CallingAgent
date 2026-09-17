"""CallAttempt -- Database Design §2.5, Call State Machine §3.

STATE, connection_failure_reason (never-connected outcome), and
disconnect_reason (mid-call outcome) are kept as three separate columns
deliberately -- they are different taxonomies and must not be collapsed
(Checkpoint 01 spec, Call State Machine §5).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import (
    CallAttemptState,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
    RecordingConsent,
)


class CallAttempt(Base):
    __tablename__ = "call_attempt"
    __table_args__ = (
        # Idempotency invariant (Checkpoint 01 spec, Step 6): a contact
        # belongs to exactly one campaign, so (contact_id, attempt_number)
        # already encodes campaign_id + contact_id + attempt_number.
        UniqueConstraint(
            "contact_id", "attempt_number", name="uq_call_attempt_contact_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contact.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[CallAttemptState] = mapped_column(
        pg_enum(CallAttemptState, "call_attempt_state"), nullable=False
    )
    connection_failure_reason: Mapped[NeverConnectedFailureReason | None] = (
        mapped_column(
            pg_enum(NeverConnectedFailureReason, "never_connected_failure_reason"),
            nullable=True,
        )
    )
    disconnect_reason: Mapped[MidCallDisconnectReason | None] = mapped_column(
        pg_enum(MidCallDisconnectReason, "mid_call_disconnect_reason"),
        nullable=True,
    )
    recording_consent: Mapped[RecordingConsent | None] = mapped_column(
        pg_enum(RecordingConsent, "recording_consent"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
