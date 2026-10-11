# Data inventory (CP15A)

Where personal data lives, who can read it, how long it is kept and how it is deleted.
The column table below is **generated from** `backend/app/core/data_inventory.py`; a test
(`tests/test_cp15a_data_inventory.py`) fails when the registry and the database schema
disagree or when this table is stale. Regenerate it with:

    cd backend && python -c "from app.core.data_inventory import render_inventory_markdown as r; print(r())"

Labels: VERIFIED (proven by a test run against real Postgres/Redis), DOCUMENTED LIMITATION,
DEFERRED.

## 1. Classifications

| Class | Meaning |
|---|---|
| PII_DIRECT | a phone number |
| PII_CONTENT | free text a person said, or text derived from what they said |
| PII_INDIRECT | identifiers, keyed fingerprints, free-text metadata that identify a person or call only when joined with something else |
| SECRET | credential-like values |
| NON_PII | everything else |

## 2. Where each class of data lives

| Location | What | Who can read | Retention | Deletion method |
|---|---|---|---|---|
| Postgres `contact` | phone numbers (PII_DIRECT) | admin / operator via the API (masked in lists); admin export | `RETENTION_CONTACT_DAYS` after the campaign is closed | scrub to a unique placeholder + `purged_at` (`run_retention`, erasure). VERIFIED |
| Postgres `suppression` | do-not-call numbers (PII_DIRECT) | admin / operator (masked) | **never purged** (a lifecycle job that deleted it would re-open a person to calls) | manual admin removal only, audited (CP14). VERIFIED that no lifecycle path touches it |
| Postgres `conversation_message`, `working_memory_snapshot` | transcript turns, captured entities, objections, last utterance (PII_CONTENT) | admin / operator via call-attempt detail; admin export | `RETENTION_TRANSCRIPT_DAYS` | turns deleted, snapshot content cleared. VERIFIED |
| Postgres `call_analysis` | summary, feedback, key facts, objections, needs, error text (PII_CONTENT) | admin / operator via analysis endpoints | `RETENTION_ANALYSIS_DAYS` | free text nulled, scores and enums kept. VERIFIED |
| Postgres `audit_log` | actor, entity ids, small metadata (PII_INDIRECT); never a full number | admin / operator | `RETENTION_AUDIT_LOG_DAYS` | deleted by age; rows about a held campaign/contact stay. VERIFIED |
| Postgres `processed_event` | provider event ids | none via API | `RETENTION_PROCESSED_EVENT_DAYS` (>= the provider retry horizon) | deleted by age. VERIFIED |
| Postgres `erasure_log` | keyed fingerprint of an erased number, actor, counts | none via API | kept (evidence) | append-only: a trigger rejects UPDATE/DELETE. VERIFIED |
| Postgres `retention_run` | per-run counts | any authenticated role (status endpoint) | `RETENTION_RUN_HISTORY_DAYS` | deleted by age. VERIFIED |
| Redis | queue jobs (ids), rate-limit counters, kill switch, heartbeats, and the **working-memory cache** (`MemoryStore`: captured entities, objections, last agent utterance = PII_CONTENT, written by the native conversation path) | internal only | working-memory cache TTL is 3600 s; the rest is ephemeral; Redis is not durable in compose | expires by TTL; **erasure and retention do not delete cache keys** (they are gone within 1 hour). DOCUMENTED LIMITATION. Payload contents of other Redis keys were not exhaustively audited: UNVERIFIED |
| Logs | request ids and sanitized event names; code review found no number/transcript logging (httpx URL logging is suppressed) | operators with log access | log platform's retention (outside this repo) | outside this repo. DEFERRED (CP16/CP19) |
| Dograh (provider side) | call audio, provider-side transcripts, run metadata | Dograh operators | provider-controlled | **not deletable from this repo; no documented delete API.** DOCUMENTED LIMITATION |
| Backups (CP15B) | everything in Postgres | backup operators | backup retention | age out; erased people are re-scrubbed after a restore by `reapply_erasures` |
| Recordings | **not stored.** Recording ingestion is BLOCKED (provider-side consent, decision 9) | n/a | n/a | n/a |

## 3. Column registry

| Table | Column | Classification | Retention class | Governing setting |
|---|---|---|---|---|
| agent_config | campaign_id | NON_PII | CONFIG | n/a (kept) |
| agent_config | escalation_contact_method | NON_PII | CONFIG | n/a (kept) |
| agent_config | goal | NON_PII | CONFIG | n/a (kept) |
| agent_config | id | NON_PII | CONFIG | n/a (kept) |
| agent_config | persona_tone | NON_PII | CONFIG | n/a (kept) |
| agent_config | prompt_template_version | NON_PII | CONFIG | n/a (kept) |
| agent_config | required_entity_fields | NON_PII | CONFIG | n/a (kept) |
| agent_config | script_skeleton | NON_PII | CONFIG | n/a (kept) |
| analysis_budget_day | day | NON_PII | OPERATIONAL | n/a (kept) |
| analysis_budget_day | reserved_cost | NON_PII | OPERATIONAL | n/a (kept) |
| analysis_budget_day | updated_at | NON_PII | OPERATIONAL | n/a (kept) |
| audit_log | action | NON_PII | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | actor | PII_INDIRECT | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | created_at | NON_PII | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | entity_id | PII_INDIRECT | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | entity_type | NON_PII | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | id | NON_PII | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| audit_log | metadata | PII_INDIRECT | AUDIT_LOG | `RETENTION_AUDIT_LOG_DAYS` |
| call_analysis | analysis_version | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | attempt_count | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | budget_day | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | call_attempt_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | campaign_id | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | claim_token | SECRET | OPERATIONAL | n/a (kept) |
| call_analysis | completed_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | contact_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | conversation_session_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | created_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | customer_needs | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | error_code | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | error_message | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | failed_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | feedback | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | id | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | input_duration_seconds | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | input_message_count | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | input_tokens | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | intent | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | interest_status | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | key_facts | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | language | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | last_enqueued_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | lead_score | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | lease_expires_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | model_name | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | model_provider | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | next_action | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | next_attempt_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | objections | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | observed_run_cost_usd | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | output_tokens | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | processing_started_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | prompt_version | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | reserved_cost | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | sentiment | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | status | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | summary | PII_CONTENT | ANALYSIS | `RETENTION_ANALYSIS_DAYS` |
| call_analysis | truncated | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_analysis | updated_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | attempt_number | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | connection_failure_reason | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | contact_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | disconnect_reason | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | dograh_workflow_id | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | duration_seconds | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | ended_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | provider | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | provider_call_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | recording_consent | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | started_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_attempt | state | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_event | call_attempt_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| call_event | event_type | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_event | id | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_event | occurred_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| call_event | payload | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| campaign | closed_at | NON_PII | CONFIG | n/a (kept) |
| campaign | created_at | NON_PII | CONFIG | n/a (kept) |
| campaign | default_region | NON_PII | CONFIG | n/a (kept) |
| campaign | id | NON_PII | CONFIG | n/a (kept) |
| campaign | legal_hold | NON_PII | CONFIG | n/a (kept) |
| campaign | name | NON_PII | CONFIG | n/a (kept) |
| campaign | status | NON_PII | CONFIG | n/a (kept) |
| campaign | timezone | NON_PII | CONFIG | n/a (kept) |
| contact | attempt_count | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | campaign_id | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | created_at | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | id | PII_INDIRECT | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | legal_hold | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | normalized_phone_number | PII_DIRECT | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | phone_number | PII_DIRECT | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | purged_at | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | status | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| contact | updated_at | NON_PII | CONTACT | `RETENTION_CONTACT_DAYS` |
| conversation_message | content | PII_CONTENT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_message | created_at | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_message | id | PII_INDIRECT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_message | role | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_message | sequence | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_message | session_id | PII_INDIRECT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| conversation_session | call_attempt_id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| conversation_session | ended_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| conversation_session | id | PII_INDIRECT | ATTEMPT_RECORD | n/a (kept) |
| conversation_session | started_at | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| conversation_session | status | NON_PII | ATTEMPT_RECORD | n/a (kept) |
| erasure_log | actor | PII_INDIRECT | ERASURE_LOG | n/a (kept) |
| erasure_log | counts | NON_PII | ERASURE_LOG | n/a (kept) |
| erasure_log | fingerprint | PII_INDIRECT | ERASURE_LOG | n/a (kept) |
| erasure_log | id | NON_PII | ERASURE_LOG | n/a (kept) |
| erasure_log | reason | PII_INDIRECT | ERASURE_LOG | n/a (kept) |
| erasure_log | requested_at | NON_PII | ERASURE_LOG | n/a (kept) |
| processed_event | event_id | PII_INDIRECT | PROCESSED_EVENT | `RETENTION_PROCESSED_EVENT_DAYS` |
| processed_event | event_type | NON_PII | PROCESSED_EVENT | `RETENTION_PROCESSED_EVENT_DAYS` |
| processed_event | id | NON_PII | PROCESSED_EVENT | `RETENTION_PROCESSED_EVENT_DAYS` |
| processed_event | received_at | NON_PII | PROCESSED_EVENT | `RETENTION_PROCESSED_EVENT_DAYS` |
| retention_run | counts | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | error_category | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | finished_at | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | id | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | mode | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | started_at | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retention_run | status | NON_PII | RETENTION_RUN_HISTORY | `RETENTION_RUN_HISTORY_DAYS` |
| retry_policy | campaign_id | NON_PII | CONFIG | n/a (kept) |
| retry_policy | id | NON_PII | CONFIG | n/a (kept) |
| retry_policy | max_retries | NON_PII | CONFIG | n/a (kept) |
| retry_policy | mid_call_rules | NON_PII | CONFIG | n/a (kept) |
| retry_policy | never_connected_rules | NON_PII | CONFIG | n/a (kept) |
| retry_policy | retry_spacing_seconds | NON_PII | CONFIG | n/a (kept) |
| retry_policy | window_end | NON_PII | CONFIG | n/a (kept) |
| retry_policy | window_start | NON_PII | CONFIG | n/a (kept) |
| suppression | contact_id | PII_INDIRECT | SUPPRESSION_NEVER | none (never purged) |
| suppression | created_by | PII_INDIRECT | SUPPRESSION_NEVER | none (never purged) |
| suppression | id | NON_PII | SUPPRESSION_NEVER | none (never purged) |
| suppression | phone_number | PII_DIRECT | SUPPRESSION_NEVER | none (never purged) |
| suppression | reason | PII_INDIRECT | SUPPRESSION_NEVER | none (never purged) |
| suppression | requested_at | NON_PII | SUPPRESSION_NEVER | none (never purged) |
| suppression | source | NON_PII | SUPPRESSION_NEVER | none (never purged) |
| working_memory_snapshot | attempt_id | PII_INDIRECT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | captured_entities | PII_CONTENT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | disconnect_count | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | id | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | last_agent_utterance | PII_CONTENT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | objections_raised | PII_CONTENT | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | recording_consent | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | requires_suppression | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | schema_version | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | script_progress | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |
| working_memory_snapshot | snapshotted_at | NON_PII | TRANSCRIPT | `RETENTION_TRANSCRIPT_DAYS` |

