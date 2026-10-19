"""CP15A -- data inventory and classification registry.

Every `table.column` in the SQLAlchemy metadata is classified here and mapped to a
retention class. `tests/test_cp15a_data_inventory.py` FAILS when a column is unclassified or
the registry names a column that no longer exists, so every future migration (including new
analysis or recording columns) must update this file. docs/DATA-INVENTORY.md embeds the
table rendered by `render_inventory_markdown()`; a test keeps the two in step.

Classifications
  PII_DIRECT    a phone number
  PII_CONTENT   free text a person said or that was derived from what they said
  PII_INDIRECT  identifiers / keyed fingerprints / free-text metadata that single out a
                person or call only when joined with something else
  SECRET        a credential-like value
  NON_PII       everything else

Retention classes
  TRANSCRIPT, ANALYSIS, CONTACT, AUDIT_LOG, PROCESSED_EVENT, RETENTION_RUN_HISTORY
                purged by `run_retention` per its RETENTION_*_DAYS setting
  ERASURE_LOG   kept: evidence that an erasure happened (fingerprints only)
  SUPPRESSION_NEVER
                the global do-not-call list: never purged by any lifecycle job
  ATTEMPT_RECORD
                operational call record (ids, states, timings): kept; carries no content
  CONFIG / OPERATIONAL
                business configuration / counters: kept
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import MetaData


class DataClass(StrEnum):
    PII_DIRECT = "PII_DIRECT"
    PII_CONTENT = "PII_CONTENT"
    PII_INDIRECT = "PII_INDIRECT"
    SECRET = "SECRET"
    NON_PII = "NON_PII"


class Retention(StrEnum):
    TRANSCRIPT = "TRANSCRIPT"
    ANALYSIS = "ANALYSIS"
    CONTACT = "CONTACT"
    AUDIT_LOG = "AUDIT_LOG"
    PROCESSED_EVENT = "PROCESSED_EVENT"
    RETENTION_RUN_HISTORY = "RETENTION_RUN_HISTORY"
    ERASURE_LOG = "ERASURE_LOG"
    SUPPRESSION_NEVER = "SUPPRESSION_NEVER"
    ATTEMPT_RECORD = "ATTEMPT_RECORD"
    CONFIG = "CONFIG"
    OPERATIONAL = "OPERATIONAL"


# Retention classes that a PII_DIRECT / PII_CONTENT column may carry: either a lifecycle job
# purges it, or it is the deliberate never-purge do-not-call list.
PURGE_CLASSES = frozenset(
    {
        Retention.TRANSCRIPT,
        Retention.ANALYSIS,
        Retention.CONTACT,
        Retention.AUDIT_LOG,
        Retention.PROCESSED_EVENT,
        Retention.RETENTION_RUN_HISTORY,
    }
)
PII_CLASSES_REQUIRING_PURGE = frozenset({DataClass.PII_DIRECT, DataClass.PII_CONTENT})
# Which setting governs each purge class (None = no setting, by design).
RETENTION_SETTING = {
    Retention.TRANSCRIPT: "RETENTION_TRANSCRIPT_DAYS",
    Retention.ANALYSIS: "RETENTION_ANALYSIS_DAYS",
    Retention.CONTACT: "RETENTION_CONTACT_DAYS",
    Retention.AUDIT_LOG: "RETENTION_AUDIT_LOG_DAYS",
    Retention.PROCESSED_EVENT: "RETENTION_PROCESSED_EVENT_DAYS",
    Retention.RETENTION_RUN_HISTORY: "RETENTION_RUN_HISTORY_DAYS",
    Retention.SUPPRESSION_NEVER: None,
}


@dataclass(frozen=True)
class Entry:
    data_class: DataClass
    retention: Retention


D, C, X, S, N = (  # X = PII_INDIRECT (a bare `I` trips E741)
    DataClass.PII_DIRECT,
    DataClass.PII_CONTENT,
    DataClass.PII_INDIRECT,
    DataClass.SECRET,
    DataClass.NON_PII,
)
R = Retention

# table -> column -> (class, retention)
_SPEC: dict[str, dict[str, tuple[DataClass, Retention]]] = {
    "agent_config": {
        "id": (N, R.CONFIG),
        "campaign_id": (N, R.CONFIG),
        "persona_tone": (N, R.CONFIG),
        "goal": (N, R.CONFIG),
        "script_skeleton": (N, R.CONFIG),
        "required_entity_fields": (N, R.CONFIG),
        "escalation_contact_method": (N, R.CONFIG),
        "prompt_template_version": (N, R.CONFIG),
    },
    "analysis_budget_day": {
        "day": (N, R.OPERATIONAL),
        "reserved_cost": (N, R.OPERATIONAL),
        "updated_at": (N, R.OPERATIONAL),
    },
    # HF1 1.3: durable kill switch / provider halt. The reason is operator free text and the
    # actor columns are usernames -- indirect identifiers, like audit_log's -- so they are
    # classified conservatively; nothing here is a phone number or call content.
    "outbound_control": {
        "key": (N, R.OPERATIONAL),
        "engaged": (N, R.OPERATIONAL),
        "reason": (X, R.OPERATIONAL),
        "category": (N, R.OPERATIONAL),
        "engaged_by": (X, R.OPERATIONAL),
        "engaged_at": (N, R.OPERATIONAL),
        "released_by": (X, R.OPERATIONAL),
        "released_at": (N, R.OPERATIONAL),
    },
    "audit_log": {
        "id": (N, R.AUDIT_LOG),
        "actor": (X, R.AUDIT_LOG),
        "action": (N, R.AUDIT_LOG),
        "entity_type": (N, R.AUDIT_LOG),
        "entity_id": (X, R.AUDIT_LOG),
        "metadata": (X, R.AUDIT_LOG),
        "created_at": (N, R.AUDIT_LOG),
    },
    "call_analysis": {
        "id": (N, R.ATTEMPT_RECORD),
        "call_attempt_id": (X, R.ATTEMPT_RECORD),
        "conversation_session_id": (X, R.ATTEMPT_RECORD),
        "contact_id": (X, R.ATTEMPT_RECORD),
        "campaign_id": (N, R.ATTEMPT_RECORD),
        "status": (N, R.ATTEMPT_RECORD),
        "attempt_count": (N, R.ATTEMPT_RECORD),
        "summary": (C, R.ANALYSIS),
        "intent": (N, R.ATTEMPT_RECORD),
        "interest_status": (N, R.ATTEMPT_RECORD),
        "sentiment": (N, R.ATTEMPT_RECORD),
        "feedback": (C, R.ANALYSIS),
        "next_action": (N, R.ATTEMPT_RECORD),
        "key_facts": (C, R.ANALYSIS),
        "objections": (C, R.ANALYSIS),
        "customer_needs": (C, R.ANALYSIS),
        "language": (N, R.ATTEMPT_RECORD),
        "lead_score": (N, R.ATTEMPT_RECORD),
        "model_provider": (N, R.ATTEMPT_RECORD),
        "model_name": (N, R.ATTEMPT_RECORD),
        "prompt_version": (N, R.ATTEMPT_RECORD),
        "analysis_version": (N, R.ATTEMPT_RECORD),
        "input_message_count": (N, R.ATTEMPT_RECORD),
        "input_duration_seconds": (N, R.ATTEMPT_RECORD),
        "input_tokens": (N, R.ATTEMPT_RECORD),
        "output_tokens": (N, R.ATTEMPT_RECORD),
        "claim_token": (S, R.OPERATIONAL),
        "lease_expires_at": (N, R.ATTEMPT_RECORD),
        "next_attempt_at": (N, R.ATTEMPT_RECORD),
        "last_enqueued_at": (N, R.ATTEMPT_RECORD),
        "truncated": (N, R.ATTEMPT_RECORD),
        "budget_day": (N, R.ATTEMPT_RECORD),
        "reserved_cost": (N, R.ATTEMPT_RECORD),
        "observed_run_cost_usd": (N, R.ATTEMPT_RECORD),
        "processing_started_at": (N, R.ATTEMPT_RECORD),
        "completed_at": (N, R.ATTEMPT_RECORD),
        "failed_at": (N, R.ATTEMPT_RECORD),
        "error_code": (N, R.ATTEMPT_RECORD),
        "error_message": (C, R.ANALYSIS),
        "created_at": (N, R.ATTEMPT_RECORD),
        "updated_at": (N, R.ATTEMPT_RECORD),
    },
    "call_attempt": {
        "id": (X, R.ATTEMPT_RECORD),
        "contact_id": (X, R.ATTEMPT_RECORD),
        "attempt_number": (N, R.ATTEMPT_RECORD),
        "state": (N, R.ATTEMPT_RECORD),
        "connection_failure_reason": (N, R.ATTEMPT_RECORD),
        "disconnect_reason": (N, R.ATTEMPT_RECORD),
        "recording_consent": (N, R.ATTEMPT_RECORD),
        "provider": (N, R.ATTEMPT_RECORD),
        "provider_call_id": (X, R.ATTEMPT_RECORD),
        "dograh_workflow_id": (N, R.ATTEMPT_RECORD),
        "started_at": (N, R.ATTEMPT_RECORD),
        "ended_at": (N, R.ATTEMPT_RECORD),
        "duration_seconds": (N, R.ATTEMPT_RECORD),
    },
    "call_event": {
        "id": (N, R.ATTEMPT_RECORD),
        "call_attempt_id": (X, R.ATTEMPT_RECORD),
        "event_type": (N, R.ATTEMPT_RECORD),
        "payload": (X, R.ATTEMPT_RECORD),
        "occurred_at": (N, R.ATTEMPT_RECORD),
    },
    "campaign": {
        "id": (N, R.CONFIG),
        "name": (N, R.CONFIG),
        "status": (N, R.CONFIG),
        "timezone": (N, R.CONFIG),
        "default_region": (N, R.CONFIG),
        "legal_hold": (N, R.CONFIG),
        "closed_at": (N, R.CONFIG),
        "created_at": (N, R.CONFIG),
    },
    "contact": {
        "id": (X, R.CONTACT),
        "campaign_id": (N, R.CONTACT),
        "phone_number": (D, R.CONTACT),
        "normalized_phone_number": (D, R.CONTACT),
        "status": (N, R.CONTACT),
        "attempt_count": (N, R.CONTACT),
        "next_retry_at": (N, R.CONTACT),
        "retry_attempt_id": (N, R.CONTACT),
        "retry_attempt_number": (N, R.CONTACT),
        "legal_hold": (N, R.CONTACT),
        "purged_at": (N, R.CONTACT),
        "created_at": (N, R.CONTACT),
        "updated_at": (N, R.CONTACT),
    },
    "conversation_message": {
        "id": (X, R.TRANSCRIPT),
        "session_id": (X, R.TRANSCRIPT),
        "sequence": (N, R.TRANSCRIPT),
        "role": (N, R.TRANSCRIPT),
        "content": (C, R.TRANSCRIPT),
        "created_at": (N, R.TRANSCRIPT),
    },
    "conversation_session": {
        "id": (X, R.ATTEMPT_RECORD),
        "call_attempt_id": (X, R.ATTEMPT_RECORD),
        "status": (N, R.ATTEMPT_RECORD),
        "started_at": (N, R.ATTEMPT_RECORD),
        "ended_at": (N, R.ATTEMPT_RECORD),
        "transcript_status": (N, R.ATTEMPT_RECORD),
        "transcript_unavailable_reason": (N, R.ATTEMPT_RECORD),
        # A signed link: kept only while retryable-unavailable; cleared on resolution, by erasure
        # and by transcript retention.
        "transcript_url": (S, R.TRANSCRIPT),
    },
    "erasure_log": {
        "id": (N, R.ERASURE_LOG),
        "fingerprint": (X, R.ERASURE_LOG),
        "requested_at": (N, R.ERASURE_LOG),
        "actor": (X, R.ERASURE_LOG),
        "reason": (X, R.ERASURE_LOG),
        "counts": (N, R.ERASURE_LOG),
    },
    "processed_event": {
        "id": (N, R.PROCESSED_EVENT),
        "event_id": (X, R.PROCESSED_EVENT),
        "event_type": (N, R.PROCESSED_EVENT),
        "received_at": (N, R.PROCESSED_EVENT),
    },
    "retention_run": {
        "id": (N, R.RETENTION_RUN_HISTORY),
        "started_at": (N, R.RETENTION_RUN_HISTORY),
        "finished_at": (N, R.RETENTION_RUN_HISTORY),
        "mode": (N, R.RETENTION_RUN_HISTORY),
        "status": (N, R.RETENTION_RUN_HISTORY),
        "counts": (N, R.RETENTION_RUN_HISTORY),
        "error_category": (N, R.RETENTION_RUN_HISTORY),
    },
    "retry_policy": {
        "id": (N, R.CONFIG),
        "campaign_id": (N, R.CONFIG),
        "max_retries": (N, R.CONFIG),
        "retry_spacing_seconds": (N, R.CONFIG),
        "never_connected_rules": (N, R.CONFIG),
        "mid_call_rules": (N, R.CONFIG),
        "window_start": (N, R.CONFIG),
        "window_end": (N, R.CONFIG),
    },
    "suppression": {
        "id": (N, R.SUPPRESSION_NEVER),
        "contact_id": (X, R.SUPPRESSION_NEVER),
        "phone_number": (D, R.SUPPRESSION_NEVER),
        "reason": (X, R.SUPPRESSION_NEVER),
        "source": (N, R.SUPPRESSION_NEVER),
        "created_by": (X, R.SUPPRESSION_NEVER),
        "requested_at": (N, R.SUPPRESSION_NEVER),
    },
    "working_memory_snapshot": {
        "id": (N, R.TRANSCRIPT),
        "attempt_id": (X, R.TRANSCRIPT),
        "captured_entities": (C, R.TRANSCRIPT),
        "script_progress": (N, R.TRANSCRIPT),
        "objections_raised": (C, R.TRANSCRIPT),
        "last_agent_utterance": (C, R.TRANSCRIPT),
        "disconnect_count": (N, R.TRANSCRIPT),
        "requires_suppression": (N, R.TRANSCRIPT),
        "recording_consent": (N, R.TRANSCRIPT),
        "schema_version": (N, R.TRANSCRIPT),
        "snapshotted_at": (N, R.TRANSCRIPT),
    },
}

REGISTRY: dict[str, Entry] = {
    f"{table}.{column}": Entry(*spec)
    for table, columns in _SPEC.items()
    for column, spec in columns.items()
}


def metadata_columns(metadata: MetaData) -> set[str]:
    return {f"{t.name}.{c.name}" for t in metadata.sorted_tables for c in t.columns}


def registry_problems(metadata: MetaData) -> list[str]:
    """Human-readable problems; empty when the registry matches the schema exactly."""
    actual = metadata_columns(metadata)
    problems = [f"unclassified column: {c}" for c in sorted(actual - REGISTRY.keys())]
    problems += [f"stale registry entry: {c}" for c in sorted(REGISTRY.keys() - actual)]
    for key, entry in sorted(REGISTRY.items()):
        if entry.data_class in PII_CLASSES_REQUIRING_PURGE and not (
            entry.retention in PURGE_CLASSES or entry.retention == Retention.SUPPRESSION_NEVER
        ):
            problems.append(f"{key}: {entry.data_class} needs a purge or never-purge retention")
        if entry.retention == Retention.SUPPRESSION_NEVER and not key.startswith("suppression."):
            problems.append(f"{key}: only suppression columns may be SUPPRESSION_NEVER")
    return problems


def render_inventory_markdown() -> str:
    """The table embedded in docs/DATA-INVENTORY.md (one row per table and class)."""
    lines = [
        "| Table | Column | Classification | Retention class | Governing setting |",
        "|---|---|---|---|---|",
    ]
    for key, entry in sorted(REGISTRY.items()):
        table, column = key.split(".", 1)
        setting = RETENTION_SETTING.get(entry.retention)
        if entry.retention == Retention.SUPPRESSION_NEVER:
            shown = "none (never purged)"
        else:
            shown = f"`{setting}`" if setting else "n/a (kept)"
        lines.append(f"| {table} | {column} | {entry.data_class} | {entry.retention} | {shown} |")
    return "\n".join(lines) + "\n"
