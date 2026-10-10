"""CallAnalysis -- Checkpoint 06 §4, post-call intelligence persistence.

One row per CallAttempt (UNIQUE(call_attempt_id) -- Checkpoint 06 §6's
idempotency invariant, enforced at the database level exactly like
CallAttempt's own (contact_id, attempt_number) constraint). PostgreSQL
is authoritative for analysis idempotency and status; Redis (the
analysis queue) is a fast path only, never the source of truth for
"was this call analyzed" -- see docs/CHECKPOINT-06-NOTES.md.

Transcript vs analysis (§11): this table stores WHAT THE SYSTEM
INFERRED, never the transcript itself -- that remains
ConversationMessage rows, read fresh by the worker and never
duplicated here.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, pg_enum
from app.models.enums import (
    AnalysisIntent,
    AnalysisNextAction,
    AnalysisSentiment,
    AnalysisStatus,
    InterestStatus,
)


class CallAnalysis(Base):
    __tablename__ = "call_analysis"
    __table_args__ = (
        # Idempotency invariant (§6): the same terminal call may be
        # admitted more than once (duplicate terminal-state paths,
        # duplicate queue deliveries) -- this constraint is what makes
        # get-or-create safe under concurrent admission.
        UniqueConstraint("call_attempt_id", name="uq_call_analysis_call_attempt"),
        # "analysis by campaign/contact" (API §37), "jobs by status"
        # (observability §31 -- oldest pending/processing job age).
        Index("ix_call_analysis_contact_id", "contact_id"),
        Index("ix_call_analysis_campaign_id", "campaign_id"),
        Index("ix_call_analysis_status", "status"),
        Index("ix_call_analysis_created_at", "created_at"),
        # CP14B sweeper discovery (bounded, indexed -- never a table scan).
        Index("ix_call_analysis_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_call_analysis_status_lease", "status", "lease_expires_at"),
        # A PROCESSING row always has an owner (fencing token) and a lease.
        CheckConstraint(
            "status <> 'processing' OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_call_analysis_processing_has_lease",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_call_analysis_attempt_count_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    call_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("call_attempt.id"), nullable=False
    )
    conversation_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_session.id"), nullable=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contact.id"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaign.id"), nullable=False)

    status: Mapped[AnalysisStatus] = mapped_column(
        pg_enum(AnalysisStatus, "analysis_status"),
        nullable=False,
        default=AnalysisStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- LLM analysis contract (§12), nullable until COMPLETED --------
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent: Mapped[AnalysisIntent | None] = mapped_column(
        pg_enum(AnalysisIntent, "analysis_intent"), nullable=True
    )
    interest_status: Mapped[InterestStatus | None] = mapped_column(
        pg_enum(InterestStatus, "interest_status"), nullable=True
    )
    sentiment: Mapped[AnalysisSentiment | None] = mapped_column(
        pg_enum(AnalysisSentiment, "analysis_sentiment"), nullable=True
    )
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action: Mapped[AnalysisNextAction | None] = mapped_column(
        pg_enum(AnalysisNextAction, "analysis_next_action"), nullable=True
    )
    key_facts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    objections: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    customer_needs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    language: Mapped[str | None] = mapped_column(String, nullable=True)

    # -- deterministic scoring (§14) -- computed from LLM-extracted
    # signals by app.services.analysis.scoring, never returned directly
    # by the LLM.
    lead_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- provenance / versioning (§19, §28) ---------------------------
    model_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_version: Mapped[str | None] = mapped_column(String, nullable=True)

    # -- input/cost accounting (§30-31) -------------------------------
    input_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- CP14B: lease + fencing, retry scheduling, publication tracking ---
    # claim_token is the FENCING token: a fresh uuid per claim. Every
    # result/retry write is `WHERE claim_token = :mine AND status =
    # 'processing'`, so a worker whose lease expired (and whose row was
    # re-claimed) can never overwrite a newer worker's outcome.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the job was last (to be) published to Redis. Redis is a fast
    # path only: the sweeper republishes a still-unfinished row whose
    # publication is older than analysis_republish_after_seconds.
    last_enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- CP14B daily estimated-spend accounting (separate from CP14's dialing cap)
    budget_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # Run-level charge as REPORTED by Dograh. NOT attributed to QA: whether
    # it includes post-call QA tokens is UNVERIFIED (see CHECKPOINT-14B-NOTES).
    observed_run_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # -- lifecycle timestamps (§5, §24) -------------------------------
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AnalysisBudgetDay(Base):
    """CP14B: durable per-day ledger of ESTIMATED post-call-analysis spend.

    One row per budget day. `reserved_cost` only moves through the atomic
    conditional upsert in app.services.analysis.budget, so concurrent
    workers cannot overspend through a race. All figures are estimates,
    never billing."""

    __tablename__ = "analysis_budget_day"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
