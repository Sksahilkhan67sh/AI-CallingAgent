"""Pydantic API schema for CallAnalysis -- Checkpoint 06 §37.
Read-only: CP06 exposes analysis results, it does not accept them
from clients."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import (
    AnalysisIntent,
    AnalysisNextAction,
    AnalysisSentiment,
    AnalysisStatus,
    InterestStatus,
)


class CallAnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    call_attempt_id: uuid.UUID
    conversation_session_id: uuid.UUID | None
    contact_id: uuid.UUID
    campaign_id: uuid.UUID
    status: AnalysisStatus

    summary: str | None
    intent: AnalysisIntent | None
    interest_status: InterestStatus | None
    sentiment: AnalysisSentiment | None
    feedback: str | None
    next_action: AnalysisNextAction | None
    key_facts: list
    objections: list
    customer_needs: list
    language: str | None
    lead_score: int | None

    model_provider: str | None
    model_name: str | None
    prompt_version: str | None
    analysis_version: str | None

    input_message_count: int | None
    input_duration_seconds: float | None

    error_code: str | None
    error_message: str | None

    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    failed_at: datetime | None
