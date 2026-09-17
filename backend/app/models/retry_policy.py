"""RetryPolicy -- Database Design §2.2.

One row per campaign; the single shared configuration read by both
never-connected and mid-call-disconnect retry evaluations. This
checkpoint establishes persistence only -- the retry scheduler itself
belongs to a later checkpoint.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Time
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DEFAULT_RETRY_SPACING_SECONDS = [30, 600]
DEFAULT_NEVER_CONNECTED_RULES = {
    "no_answer": True,
    "busy": True,
    "invalid_number": False,
    "rejected": False,
    "network_error": True,
    "provider_error": True,
}
DEFAULT_MID_CALL_RULES = {
    "technical_issue": True,
    "network_problem": True,
    "provider_error": True,
    "ai_error": True,
    "unknown": True,
    "customer_hangup": False,
}


class RetryPolicy(Base):
    __tablename__ = "retry_policy"
    __table_args__ = (
        CheckConstraint(
            "jsonb_array_length(retry_spacing_seconds) = max_retries",
            name="ck_retry_policy_spacing_matches_max_retries",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaign.id"), nullable=False, unique=True
    )
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    retry_spacing_seconds: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_RETRY_SPACING_SECONDS)
    )
    never_connected_rules: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_NEVER_CONNECTED_RULES)
    )
    mid_call_rules: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_MID_CALL_RULES)
    )
    window_start: Mapped[object] = mapped_column(Time, nullable=False, default="10:00")
    window_end: Mapped[object] = mapped_column(Time, nullable=False, default="18:00")
