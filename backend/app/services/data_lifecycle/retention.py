"""Retention engine -- CP15A.

Per data class, independently configurable (`RETENTION_*_DAYS`; unset in development or 0 =
keep forever). A run is DRY-RUN unless `apply=True`; it takes a Postgres advisory lock so two
runs can never overlap, works in bounded batches (each its own transaction) under a runtime
budget, and is idempotent and resumable because every class selects "rows that still need
the action" -- re-running after a crash simply continues.

Legal hold (campaign or contact) excludes the covered rows from EVERY class.

This module must never touch the global do-not-call list (`suppression`). The guard test
`tests/test_cp15a_retention.py::test_lifecycle_modules_never_reference_suppression` fails
if the word appears in the purge code paths.

Time is injected (`now`, `monotonic`) wherever expiry or the runtime budget is decided.
Audit rows and `retention_run` rows carry COUNTS ONLY.
"""

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import (
    Engine,
    Select,
    Text,
    and_,
    cast,
    delete,
    exists,
    func,
    not_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import AnalysisStatus, CampaignStatus
from app.models.processed_event import ProcessedEvent
from app.models.retention_run import RetentionRun
from app.models.working_memory_snapshot import WorkingMemorySnapshot
from app.services.audit_service import record_audit_event
from app.services.data_lifecycle.fingerprint import PLACEHOLDER_PREFIX
from app.services.data_lifecycle.holds import held_campaign_ids, held_contact_ids
from app.services.data_lifecycle.sql_util import rowcount

logger = logging.getLogger("retention")

# Arbitrary, fixed 64-bit key for the advisory lock ("CP15" in ASCII).
ADVISORY_LOCK_KEY = 0x43503135
_NON_TERMINAL_ANALYSIS = (
    AnalysisStatus.PENDING,
    AnalysisStatus.PROCESSING,
    AnalysisStatus.RETRY_WAIT,
)
_TERMINAL_ANALYSIS = (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED, AnalysisStatus.SKIPPED)


class RetentionLockHeld(RuntimeError):
    """Another retention run holds the advisory lock."""


@dataclass(frozen=True)
class RetentionConfig:
    transcript_days: int | None = None
    analysis_days: int | None = None
    contact_days: int | None = None
    audit_log_days: int | None = None
    processed_event_days: int | None = None
    run_history_days: int | None = None
    batch_size: int = 500
    max_runtime_seconds: int = 300

    @classmethod
    def from_settings(cls, s: Settings) -> "RetentionConfig":
        return cls(
            transcript_days=s.retention_transcript_days,
            analysis_days=s.retention_analysis_days,
            contact_days=s.retention_contact_days,
            audit_log_days=s.retention_audit_log_days,
            processed_event_days=s.retention_processed_event_days,
            run_history_days=s.retention_run_history_days,
            batch_size=s.retention_batch_size,
            max_runtime_seconds=s.retention_max_runtime_seconds,
        )


@dataclass
class RetentionResult:
    run_id: uuid.UUID
    mode: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    disabled: list[str] = field(default_factory=list)
    error_category: str | None = None


@dataclass(frozen=True)
class _Step:
    key: str
    days_attr: str
    # (now, current_run_id) -> SELECT of primary keys still needing the action
    candidates: Callable[[datetime, uuid.UUID | None], Select]
    apply: Callable[[Session, list[uuid.UUID], datetime], int]


# -- per-class candidate queries and actions ---------------------------------------------------


def _transcript_messages(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return (
        select(ConversationMessage.id)
        .join(ConversationSession, ConversationSession.id == ConversationMessage.session_id)
        .join(CallAttempt, CallAttempt.id == ConversationSession.call_attempt_id)
        .where(
            ConversationMessage.created_at < cutoff,
            CallAttempt.contact_id.not_in(held_contact_ids()),
            # Never delete the input of an analysis that has not finished yet.
            ~exists().where(
                CallAnalysis.call_attempt_id == CallAttempt.id,
                CallAnalysis.status.in_(_NON_TERMINAL_ANALYSIS),
            ),
        )
    )


def _delete_messages(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(db.execute(delete(ConversationMessage).where(ConversationMessage.id.in_(ids))))


def _transcript_links(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    """P1-8: a signed transcript link retained for a retry of an UNAVAILABLE transcript."""
    return (
        select(ConversationSession.id)
        .join(CallAttempt, CallAttempt.id == ConversationSession.call_attempt_id)
        .where(
            ConversationSession.transcript_url.is_not(None),
            ConversationSession.started_at < cutoff,
            CallAttempt.contact_id.not_in(held_contact_ids()),
        )
    )


def _clear_transcript_links(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(
        db.execute(
            update(ConversationSession)
            .where(ConversationSession.id.in_(ids))
            .values(transcript_url=None)
        )
    )


def _snapshots(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return (
        select(WorkingMemorySnapshot.id)
        .join(CallAttempt, CallAttempt.id == WorkingMemorySnapshot.attempt_id)
        .where(
            WorkingMemorySnapshot.snapshotted_at < cutoff,
            CallAttempt.contact_id.not_in(held_contact_ids()),
            or_(
                cast(WorkingMemorySnapshot.captured_entities, Text) != "{}",
                cast(WorkingMemorySnapshot.objections_raised, Text) != "[]",
                WorkingMemorySnapshot.last_agent_utterance.is_not(None),
            ),
        )
    )


def _scrub_snapshots(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(db.execute(
        update(WorkingMemorySnapshot)
        .where(WorkingMemorySnapshot.id.in_(ids))
        .values(
            captured_entities=text("'{}'::jsonb"),
            objections_raised=text("'[]'::jsonb"),
            last_agent_utterance=None,
        )
    ))


def _analysis(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return select(CallAnalysis.id).where(
        CallAnalysis.created_at < cutoff,
        CallAnalysis.status.in_(_TERMINAL_ANALYSIS),
        CallAnalysis.contact_id.not_in(held_contact_ids()),
        or_(
            CallAnalysis.summary.is_not(None),
            CallAnalysis.feedback.is_not(None),
            CallAnalysis.error_message.is_not(None),
            cast(CallAnalysis.key_facts, Text) != "[]",
            cast(CallAnalysis.objections, Text) != "[]",
            cast(CallAnalysis.customer_needs, Text) != "[]",
        ),
    )


def _scrub_analysis(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    # Free text and evidence only; scores, enums and provenance stay.
    return rowcount(db.execute(
        update(CallAnalysis)
        .where(CallAnalysis.id.in_(ids))
        .values(
            summary=None,
            feedback=None,
            error_message=None,
            key_facts=text("'[]'::jsonb"),
            objections=text("'[]'::jsonb"),
            customer_needs=text("'[]'::jsonb"),
            updated_at=CallAnalysis.updated_at,  # a purge is not an analysis update
        )
    ))


def _contacts(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return (
        select(Contact.id)
        .join(Campaign, Campaign.id == Contact.campaign_id)
        .where(
            Campaign.status == CampaignStatus.COMPLETED,
            Campaign.closed_at.is_not(None),  # unknown closure date => never auto-purged
            Campaign.closed_at < cutoff,
            Campaign.legal_hold.is_(False),
            Contact.legal_hold.is_(False),
            Contact.purged_at.is_(None),
        )
    )


def scrub_contacts(db: Session, contact_ids: list[uuid.UUID], now: datetime) -> int:
    """Replace the phone fields with a unique non-reversible placeholder (so the unique
    constraint still holds), set purged_at, keep status and counters. `updated_at` is pinned
    so the scrub itself never moves any age-based decision."""
    placeholder = func.concat(PLACEHOLDER_PREFIX, func.replace(cast(Contact.id, Text), "-", ""))
    return rowcount(db.execute(
        update(Contact)
        .where(Contact.id.in_(contact_ids))
        .values(
            phone_number=placeholder,
            normalized_phone_number=placeholder,
            purged_at=now,
            updated_at=Contact.updated_at,
        )
    ))


def _audit(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return select(AuditLog.id).where(
        AuditLog.created_at < cutoff,
        # Rows about a held contact/campaign stay while the hold applies.
        or_(
            AuditLog.entity_id.is_(None),
            and_(
                not_(AuditLog.entity_id.in_(held_contact_ids())),
                not_(AuditLog.entity_id.in_(held_campaign_ids())),
            ),
        ),
    )


def _delete_audit(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(db.execute(delete(AuditLog).where(AuditLog.id.in_(ids))))


def _processed(cutoff: datetime, _run: uuid.UUID | None) -> Select:
    return select(ProcessedEvent.id).where(ProcessedEvent.received_at < cutoff)


def _delete_processed(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(db.execute(delete(ProcessedEvent).where(ProcessedEvent.id.in_(ids))))


def _run_history(cutoff: datetime, current: uuid.UUID | None) -> Select:
    stmt = select(RetentionRun.id).where(RetentionRun.started_at < cutoff)
    if current is not None:
        stmt = stmt.where(RetentionRun.id != current)
    return stmt


def _delete_runs(db: Session, ids: list[uuid.UUID], _now: datetime) -> int:
    return rowcount(db.execute(delete(RetentionRun).where(RetentionRun.id.in_(ids))))


_STEPS: tuple[_Step, ...] = (
    _Step("transcript_messages", "transcript_days", _transcript_messages, _delete_messages),
    _Step("transcript_links", "transcript_days", _transcript_links, _clear_transcript_links),
    _Step("working_memory_snapshots", "transcript_days", _snapshots, _scrub_snapshots),
    _Step("analysis_free_text", "analysis_days", _analysis, _scrub_analysis),
    _Step("contacts", "contact_days", _contacts, scrub_contacts),
    _Step("audit_log", "audit_log_days", _audit, _delete_audit),
    _Step("processed_events", "processed_event_days", _processed, _delete_processed),
    _Step("retention_runs", "run_history_days", _run_history, _delete_runs),
)
STEP_KEYS = tuple(step.key for step in _STEPS)


def _days(config: RetentionConfig, attr: str) -> int | None:
    value = getattr(config, attr)
    return value if value else None  # None and 0 both mean "keep forever"


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


def _count(db: Session, stmt: Select) -> int:
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def backlog_counts(
    db: Session, config: RetentionConfig, now: datetime, current_run: uuid.UUID | None = None
) -> dict[str, int | None]:
    """Rows currently past their retention period, per step. None = class disabled."""
    out: dict[str, int | None] = {}
    for step in _STEPS:
        days = _days(config, step.days_attr)
        out[step.key] = None if days is None else _count(
            db, step.candidates(_cutoff(now, days), current_run)
        )
    return out


# -- locking -----------------------------------------------------------------------------------


@contextmanager
def advisory_lock(engine: Engine) -> Iterator[bool]:
    """Session-level Postgres advisory lock on a DEDICATED autocommit connection, so it
    survives the per-batch commits of the working session and dies with the connection."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        got = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}).scalar()
        )
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    finally:
        conn.close()


# -- the run -----------------------------------------------------------------------------------


def _finish(
    db: Session, run: RetentionRun, result: RetentionResult, now_finished: datetime, actor: str
) -> None:
    run.finished_at = now_finished
    run.status = result.status
    run.counts = {**result.counts, "disabled_classes": result.disabled}
    run.error_category = result.error_category
    record_audit_event(
        db,
        actor=actor,
        action="retention.run",
        entity_type="retention_run",
        entity_id=run.id,
        # COUNTS ONLY: no identifiers, no PII.
        metadata={
            "mode": result.mode,
            "status": result.status,
            "counts": result.counts,
            "disabled_classes": result.disabled,
        },
    )
    db.commit()


def run_retention(
    db: Session,
    *,
    lock_engine: Engine,
    config: RetentionConfig,
    now: datetime,
    apply: bool = False,
    actor: str = "system:retention",
    monotonic: Callable[[], float] = time.monotonic,
) -> RetentionResult:
    mode = "apply" if apply else "dry_run"
    with advisory_lock(lock_engine) as got:
        run = RetentionRun(id=uuid.uuid4(), mode=mode, status="running", counts={})
        db.add(run)
        db.commit()
        result = RetentionResult(run_id=run.id, mode=mode, status="running")
        if not got:
            result.status = "failed"
            result.error_category = "lock_held"
            _finish(db, run, result, now, actor)
            raise RetentionLockHeld("another retention run holds the advisory lock")

        started = monotonic()
        try:
            for step in _STEPS:
                days = _days(config, step.days_attr)
                if days is None:
                    result.disabled.append(step.key)
                    continue
                stmt = step.candidates(_cutoff(now, days), run.id)
                if not apply:
                    result.counts[step.key] = _count(db, stmt)
                    continue
                total = 0
                while True:
                    if monotonic() - started >= config.max_runtime_seconds:
                        result.status = "partial"
                        break
                    ids = list(db.execute(stmt.limit(config.batch_size)).scalars())
                    if not ids:
                        break
                    total += step.apply(db, ids, now)
                    db.commit()
                result.counts[step.key] = total
                if result.status == "partial":
                    break
            if result.status == "running":
                result.status = "completed"
        except Exception as exc:  # sanitized category only; the detail goes to the log
            db.rollback()
            result.status = "failed"
            result.error_category = "db_error" if isinstance(exc, SQLAlchemyError) else "internal"
            logger.error("retention_run_failed category=%s", result.error_category)
        db.refresh(run)  # the rollback above may have expired it
        _finish(db, run, result, now, actor)
        return result
