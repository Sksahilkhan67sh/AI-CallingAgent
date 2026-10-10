"""Pending-analysis sweeper -- CP14B §12. PostgreSQL discovers; Redis only delivers.

Redis is a fast path, never the record of "this call still needs analysis". Every unfinished
CallAnalysis row is rediscoverable here from indexed PostgreSQL state, so work survives a Redis
restart, a failed post-commit publish, a crashed worker, an expired lease and a worker that
was stopped mid-retry.

One `sweep()` does, in order (each step bounded by `analysis_sweeper_batch_size`):

  1. FINALIZE   rows that can never be claimed again (attempts used up, no live owner) ->
                FAILED (lease_expired_attempts_exhausted). Never resets a permanent failure.
  2. REGISTER   completed, eligible calls from the last `analysis_sweeper_lookback_hours`
                that have NO analysis row (e.g. registration failed after the call committed).
                A bounded safety net -- deliberately NOT a historical backfill.
  3. PUBLISH    due PENDING / RETRY_WAIT rows and PROCESSING rows with an expired lease, whose
                last publication is missing, older than `analysis_republish_after_seconds`, or
                older than their `next_attempt_at`. Selected FOR UPDATE SKIP LOCKED so several
                sweepers (one per worker process) never publish the same row twice in a batch.

Truthfulness: `last_enqueued_at` is written only for rows whose XADD actually succeeded. If
Redis is down nothing is marked published; the rows stay discoverable for the next sweep.
Duplicate publication (crash between XADD and commit) is harmless: claims are atomic.
Budget-deferred rows carry a future `next_attempt_at`, so they are rechecked at the
configured low frequency instead of every sweep.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import AnalysisStatus
from app.services.analysis.admission import (
    _ELIGIBLE_ATTEMPT_STATES,
    _ELIGIBLE_CONTACT_STATUSES,
    job_for,
)
from app.services.analysis.queue import AnalysisQueue
from app.services.analysis.repository import CallAnalysisRepository, utcnow
from app.services.analysis.transcript import load_conversation_session
from app.services.audit_service import record_audit_event

logger = logging.getLogger("analysis.sweeper")

_ACTOR = "analysis-sweeper"


@dataclass
class SweepReport:
    finalized: int = 0
    registered: int = 0
    published: int = 0
    publish_error: bool = False
    pending_count: int = 0
    oldest_pending_age_seconds: float | None = None


def sweep(db: Session, queue: AnalysisQueue, *, now: datetime | None = None) -> SweepReport:
    settings = get_settings()
    now = now or utcnow()
    batch = settings.analysis_sweeper_batch_size
    repo = CallAnalysisRepository(db)
    report = SweepReport()

    # 1. FINALIZE
    report.finalized = repo.finalize_exhausted(
        now=now, max_attempts=settings.analysis_max_attempts, limit=batch
    )
    db.commit()

    # 2. REGISTER (bounded lookback)
    report.registered = _register_missing(db, repo, now, batch)
    db.commit()

    # 3. PUBLISH
    report.published, report.publish_error = _publish_due(db, queue, now, batch)
    db.commit()

    # Observability: pending depth + oldest pending age (indexed on status).
    pending_filter = CallAnalysis.status.in_(
        [AnalysisStatus.PENDING, AnalysisStatus.RETRY_WAIT, AnalysisStatus.PROCESSING]
    )
    count, oldest = db.execute(
        select(func.count(), func.min(CallAnalysis.created_at)).where(pending_filter)
    ).one()
    report.pending_count = int(count)
    if oldest is not None:
        report.oldest_pending_age_seconds = (now - oldest).total_seconds()
    db.commit()

    logger.info(
        "analysis_sweep",
        extra={
            "finalized": report.finalized,
            "registered": report.registered,
            "published": report.published,
            "publish_error": report.publish_error,
            "pending_count": report.pending_count,
            "oldest_pending_age_seconds": report.oldest_pending_age_seconds,
        },
    )
    return report


def _register_missing(db: Session, repo: CallAnalysisRepository, now: datetime, batch: int) -> int:
    since = now - timedelta(hours=get_settings().analysis_sweeper_lookback_hours)
    rows = db.execute(
        select(CallAttempt, Contact)
        .join(Contact, Contact.id == CallAttempt.contact_id)
        .where(
            CallAttempt.state.in_(_ELIGIBLE_ATTEMPT_STATES),
            Contact.status.in_(_ELIGIBLE_CONTACT_STATUSES),
            CallAttempt.ended_at.is_not(None),
            CallAttempt.ended_at >= since,
            ~exists().where(CallAnalysis.call_attempt_id == CallAttempt.id),
        )
        .order_by(CallAttempt.ended_at, CallAttempt.id)
        .limit(batch)
    ).all()
    registered = 0
    for attempt, contact in rows:
        session = load_conversation_session(db, attempt.id)
        analysis, created = repo.get_or_create_pending(
            call_attempt_id=attempt.id,
            contact_id=contact.id,
            campaign_id=contact.campaign_id,
            conversation_session_id=session.id if session is not None else None,
            # last_enqueued_at stays NULL: the publish step picks it up in this same sweep.
        )
        if not created:
            continue
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type="ANALYSIS_QUEUED",
                payload={"analysis_id": str(analysis.id), "source": "sweeper"},
            )
        )
        record_audit_event(
            db,
            actor=_ACTOR,
            action="analysis.registered_by_sweeper",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"analysis_id": str(analysis.id)},
        )
        registered += 1
    db.flush()
    return registered


def _publish_due(db: Session, queue: AnalysisQueue, now: datetime, batch: int) -> tuple[int, bool]:
    settings = get_settings()
    stale_before = now - timedelta(seconds=settings.analysis_republish_after_seconds)
    unpublished_or_old = or_(
        CallAnalysis.last_enqueued_at.is_(None),
        CallAnalysis.last_enqueued_at < stale_before,
        and_(
            CallAnalysis.next_attempt_at.is_not(None),
            CallAnalysis.last_enqueued_at < CallAnalysis.next_attempt_at,
        ),
    )
    due = or_(
        and_(
            CallAnalysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RETRY_WAIT]),
            or_(CallAnalysis.next_attempt_at.is_(None), CallAnalysis.next_attempt_at <= now),
            unpublished_or_old,
        ),
        and_(
            CallAnalysis.status == AnalysisStatus.PROCESSING,
            CallAnalysis.lease_expires_at < now,
            CallAnalysis.attempt_count < settings.analysis_max_attempts,
        ),
    )
    rows = list(
        db.execute(
            select(CallAnalysis)
            .where(due)
            .order_by(func.coalesce(CallAnalysis.next_attempt_at, CallAnalysis.created_at))
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    published = 0
    for analysis in rows:
        try:
            queue.enqueue(job_for(analysis))
        except Exception:
            # Redis unavailable: mark nothing as published, keep what already succeeded.
            logger.warning(
                "analysis_sweeper_publish_failed", extra={"remaining": len(rows) - published}
            )
            db.flush()
            return published, True
        analysis.last_enqueued_at = now
        published += 1
    db.flush()
    return published, False
