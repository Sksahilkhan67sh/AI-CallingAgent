"""Pydantic schemas for the admin dashboard API -- Checkpoint 07."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import (
    AnalysisIntent,
    AnalysisNextAction,
    AnalysisSentiment,
    AnalysisStatus,
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    ConversationRole,
    InterestStatus,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    username: str


# --- Dashboard overview (§9) -------------------------------------------


class CallOverview(BaseModel):
    total: int
    active: int  # Dialing/InConversation/Reconnecting
    completed: int
    completed_partial: int
    retry_scheduled: int
    closed: int
    queued: int  # pending calls:outbound stream length


class CampaignOverview(BaseModel):
    active: int
    paused: int
    completed: int
    draft: int


class IntelligenceOverview(BaseModel):
    analyzed: int
    interested: int
    maybe: int
    not_interested: int
    unknown: int
    average_lead_score: float | None


class ReliabilityOverview(BaseModel):
    retry_rate: float | None
    failure_rate: float | None
    analysis_failure_rate: float | None
    analysis_queue_depth: int
    outbound_queue_depth: int


class DashboardOverview(BaseModel):
    calls: CallOverview
    campaigns: CampaignOverview
    intelligence: IntelligenceOverview
    reliability: ReliabilityOverview


# --- Analytics (§23-24) --------------------------------------------------


class AnalyticsResponse(BaseModel):
    range: str
    campaign_id: uuid.UUID | None
    total_dial_attempts: int
    connected_attempts: int
    connection_rate: float | None
    completed_attempts: int
    completion_rate: float | None
    attempts_requiring_recovery: int
    eligible_connected_attempts: int
    retry_rate: float | None
    opt_out_count: int
    opt_out_rate: float | None
    average_duration_seconds: float | None
    analyzed_calls: int
    analysis_jobs_total: int
    analysis_completion_rate: float | None
    interested_calls: int
    interest_rate: float | None
    average_lead_score: float | None


# --- System health (§25-26) ----------------------------------------------


class ComponentHealth(BaseModel):
    name: str
    status: str  # "ok" | "degraded" | "unknown"
    detail: str | None = None


class QueueHealth(BaseModel):
    name: str
    pending: int
    processing: int
    oldest_pending_seconds: float | None


class SystemHealthResponse(BaseModel):
    components: list[ComponentHealth]
    queues: list[QueueHealth]


# --- Call attempts (§15-17, §21) -----------------------------------------


class CallAttemptListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    contact_phone_masked: str
    campaign_id: uuid.UUID
    campaign_name: str
    attempt_number: int
    state: CallAttemptState
    disconnect_reason: MidCallDisconnectReason | None
    connection_failure_reason: NeverConnectedFailureReason | None
    provider: str | None
    started_at: datetime
    ended_at: datetime | None
    analysis_status: AnalysisStatus | None
    lead_score: int | None


class TranscriptLine(BaseModel):
    role: ConversationRole
    content: str
    created_at: datetime


class RecoveryEvent(BaseModel):
    event_type: str
    payload: dict | None
    occurred_at: datetime


class CallAttemptAnalysis(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    analysis_version: str | None


class CallAttemptDetail(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_phone_masked: str
    campaign_id: uuid.UUID
    campaign_name: str
    attempt_number: int
    state: CallAttemptState
    disconnect_reason: MidCallDisconnectReason | None
    connection_failure_reason: NeverConnectedFailureReason | None
    provider: str | None
    provider_call_id: str | None
    started_at: datetime
    ended_at: datetime | None
    transcript: list[TranscriptLine]
    analysis: CallAttemptAnalysis | None
    recovery_events: list[RecoveryEvent]


# --- Contacts (§13-14) -----------------------------------------------------


class ContactListItem(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    phone_masked: str
    status: ContactStatus
    attempt_count: int
    suppressed: bool
    created_at: datetime


class ContactDetail(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    phone_masked: str
    status: ContactStatus
    attempt_count: int
    suppressed: bool
    suppression_reason: str | None
    created_at: datetime
    updated_at: datetime


# --- Campaigns (§11-12) -----------------------------------------------------


class CampaignListItem(BaseModel):
    id: uuid.UUID
    name: str
    status: CampaignStatus
    created_at: datetime
    contact_count: int


class CampaignMetrics(BaseModel):
    contacts: int
    attempts: int
    completed: int
    completed_partial: int
    active: int
    retry_scheduled: int
    interested: int
    average_lead_score: float | None


class CampaignDetail(BaseModel):
    id: uuid.UUID
    name: str
    status: CampaignStatus
    created_at: datetime
    metrics: CampaignMetrics
