// Checkpoint 07 -- types mirror app/schemas/admin.py exactly. Kept
// hand-written rather than generated: no OpenAPI-generation strategy
// exists yet in this repository (see docs/CHECKPOINT-07-NOTES.md).

export type CallAttemptState =
  | "Initiated"
  | "Connected"
  | "FailedToConnect"
  | "DroppedMidCall"
  | "EndedNormally";

export type ContactStatus =
  | "Pending"
  | "Dialing"
  | "InConversation"
  | "Disconnected"
  | "RetryScheduled"
  | "Reconnecting"
  | "Completed"
  | "CompletedPartial"
  | "Closed";

export type CampaignStatus = "draft" | "active" | "paused" | "completed";

export type AnalysisStatus = "pending" | "processing" | "completed" | "failed";
export type AnalysisIntent =
  | "interested"
  | "not_interested"
  | "information_requested"
  | "callback_requested"
  | "unclear";
export type InterestStatus = "interested" | "maybe" | "not_interested" | "unknown";
export type AnalysisSentiment = "positive" | "neutral" | "negative" | "mixed" | "unknown";
export type AnalysisNextAction =
  | "follow_up"
  | "callback"
  | "send_information"
  | "sales_contact"
  | "no_action"
  | "manual_review";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: "admin" | "operator";
  username: string;
}

export interface CallOverview {
  total: number;
  active: number;
  completed: number;
  completed_partial: number;
  retry_scheduled: number;
  closed: number;
  queued: number;
}

export interface CampaignOverview {
  active: number;
  paused: number;
  completed: number;
  draft: number;
}

export interface IntelligenceOverview {
  analyzed: number;
  interested: number;
  maybe: number;
  not_interested: number;
  unknown: number;
  average_lead_score: number | null;
}

export interface ReliabilityOverview {
  retry_rate: number | null;
  failure_rate: number | null;
  analysis_failure_rate: number | null;
  analysis_queue_depth: number;
  outbound_queue_depth: number;
}

export interface DashboardOverview {
  calls: CallOverview;
  campaigns: CampaignOverview;
  intelligence: IntelligenceOverview;
  reliability: ReliabilityOverview;
}

export interface AnalyticsResponse {
  range: string;
  campaign_id: string | null;
  total_dial_attempts: number;
  connected_attempts: number;
  connection_rate: number | null;
  completed_attempts: number;
  completion_rate: number | null;
  attempts_requiring_recovery: number;
  eligible_connected_attempts: number;
  retry_rate: number | null;
  opt_out_count: number;
  opt_out_rate: number | null;
  average_duration_seconds: number | null;
  analyzed_calls: number;
  analysis_jobs_total: number;
  analysis_completion_rate: number | null;
  interested_calls: number;
  interest_rate: number | null;
  average_lead_score: number | null;
}

export interface ComponentHealth {
  name: string;
  status: "ok" | "degraded" | "unknown";
  detail: string | null;
}

export interface QueueHealth {
  name: string;
  pending: number;
  processing: number;
  oldest_pending_seconds: number | null;
}

export interface SystemHealthResponse {
  components: ComponentHealth[];
  queues: QueueHealth[];
}

export interface CallAttemptListItem {
  id: string;
  contact_id: string;
  contact_phone_masked: string;
  campaign_id: string;
  campaign_name: string;
  attempt_number: number;
  state: CallAttemptState;
  disconnect_reason: string | null;
  connection_failure_reason: string | null;
  provider: string | null;
  started_at: string;
  ended_at: string | null;
  analysis_status: AnalysisStatus | null;
  lead_score: number | null;
}

export interface TranscriptLine {
  role: "agent" | "contact" | "system";
  content: string;
  created_at: string;
}

export interface RecoveryEvent {
  event_type: string;
  payload: Record<string, unknown> | null;
  occurred_at: string;
}

export interface CallAttemptAnalysis {
  status: AnalysisStatus;
  summary: string | null;
  intent: AnalysisIntent | null;
  interest_status: InterestStatus | null;
  sentiment: AnalysisSentiment | null;
  feedback: string | null;
  next_action: AnalysisNextAction | null;
  key_facts: string[];
  objections: string[];
  customer_needs: string[];
  language: string | null;
  lead_score: number | null;
  analysis_version: string | null;
}

export interface CallAttemptDetail {
  id: string;
  contact_id: string;
  contact_phone_masked: string;
  campaign_id: string;
  campaign_name: string;
  attempt_number: number;
  state: CallAttemptState;
  disconnect_reason: string | null;
  connection_failure_reason: string | null;
  provider: string | null;
  provider_call_id: string | null;
  started_at: string;
  ended_at: string | null;
  transcript: TranscriptLine[];
  analysis: CallAttemptAnalysis | null;
  recovery_events: RecoveryEvent[];
}

export interface ContactListItem {
  id: string;
  campaign_id: string;
  phone_masked: string;
  status: ContactStatus;
  attempt_count: number;
  suppressed: boolean;
  created_at: string;
}

export interface ContactDetail {
  id: string;
  campaign_id: string;
  campaign_name: string;
  phone_masked: string;
  status: ContactStatus;
  attempt_count: number;
  suppressed: boolean;
  suppression_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface CampaignListItem {
  id: string;
  name: string;
  status: CampaignStatus;
  created_at: string;
  contact_count: number;
}

export interface CampaignMetrics {
  contacts: number;
  attempts: number;
  completed: number;
  completed_partial: number;
  active: number;
  retry_scheduled: number;
  interested: number;
  average_lead_score: number | null;
}

export interface CampaignDetail {
  id: string;
  name: string;
  status: CampaignStatus;
  created_at: string;
  metrics: CampaignMetrics;
}
