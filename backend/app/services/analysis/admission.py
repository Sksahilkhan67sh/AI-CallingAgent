"""Analysis admission -- Checkpoint 06 §3, §9; durable-registration semantics by CP14B.

The single authoritative integration point that decides whether a just-terminalized
CallAttempt is eligible for post-call analysis, and if so, admits it.

Eligibility mirrors the repository's own terminal-state model exactly
(Core-Production-Logic.md §26-27):

  - The attempt must have reached Connected (ENDED_NORMALLY / DROPPED_MID_CALL), never
    FAILED_TO_CONNECT -- a call that never connected has no conversation.
  - The Contact's resulting status must be COMPLETED or COMPLETED_PARTIAL, never CLOSED
    (never-connected-retries-exhausted, or opted out during the call).

CP14B ordering guarantee -- "never enqueue before the state is committed":

  1. `enqueue_call_analysis` only REGISTERS durable state (the CallAnalysis row, its
     CallEvent and audit entry) inside the caller's open transaction. It touches Redis not at all.
  2. The Redis publish happens in an `after_commit` hook, i.e. only once the caller's
     transaction has really committed. A worker can therefore never read a job whose row is
     invisible, and a rolled-back terminalization cannot leave a live job behind.
  3. A failing publish (Redis down) is logged and swallowed: it must never break call
     terminalization. The row is already durable and the sweeper
     (app/services/analysis/sweeper.py) republishes it. Worst case a lost publish delays the
     analysis by `analysis_republish_after_seconds`; it is never lost.

Admission never touches retry/campaign/suppression state (§0).
"""

import logging
from datetime import timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.job import AnalysisJob
from app.services.analysis.repository import CallAnalysisRepository, utcnow
from app.services.analysis.transcript import load_conversation_session
from app.services.audit_service import record_audit_event

logger = logging.getLogger("analysis.admission")

_ACTOR = "analysis-admission"

_ELIGIBLE_ATTEMPT_STATES = (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
_ELIGIBLE_CONTACT_STATUSES = (ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL)


def is_eligible(call_attempt: CallAttempt, contact: Contact) -> bool:
    return (
        call_attempt.state in _ELIGIBLE_ATTEMPT_STATES
        and contact.status in _ELIGIBLE_CONTACT_STATUSES
    )


def job_for(analysis: CallAnalysis) -> AnalysisJob:
    return AnalysisJob.new(
        analysis_id=analysis.id,
        call_attempt_id=analysis.call_attempt_id,
        contact_id=analysis.contact_id,
        campaign_id=analysis.campaign_id,
        conversation_session_id=analysis.conversation_session_id,
    )


def enqueue_call_analysis(db: Session, call_attempt: CallAttempt, contact: Contact) -> None:
    if not is_eligible(call_attempt, contact):
        return

    session = load_conversation_session(db, call_attempt.id)
    now = utcnow()
    delay = get_settings().analysis_initial_delay_seconds

    analysis, created = CallAnalysisRepository(db).get_or_create_pending(
        call_attempt_id=call_attempt.id,
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        conversation_session_id=session.id if session is not None else None,
        # Marks "publication expected now": the sweeper only republishes if it is still
        # unfinished after analysis_republish_after_seconds.
        last_enqueued_at=now,
        next_attempt_at=now + timedelta(seconds=delay) if delay else None,
    )
    if not created:
        # Already admitted (duplicate terminal-state path / delivery) -- idempotent no-op:
        # no second row, no second job (§6/§9).
        return

    job = job_for(analysis)
    db.add(
        CallEvent(
            call_attempt_id=call_attempt.id,
            event_type="ANALYSIS_QUEUED",
            payload={"analysis_id": str(analysis.id), "trace_id": job.trace_id},
        )
    )
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.queued",
        entity_type="call_attempt",
        entity_id=call_attempt.id,
        metadata={"analysis_id": str(analysis.id)},
    )
    db.flush()
    _publish_after_commit(db, job)


def _publish_after_commit(db: Session, job: AnalysisJob) -> None:
    def _publish(_session: Session) -> None:
        try:
            get_analysis_queue().enqueue(job)
        except Exception:
            # Never break call terminalization. The committed row is recovered by the sweeper.
            logger.warning(
                "analysis_publish_failed_after_commit",
                extra={"analysis_id": job.analysis_id, "trace_id": job.trace_id},
            )

    # once=True: fires for the next successful commit of THIS session only. If the caller
    # rolls back instead, a later commit could publish a job whose row never existed; the
    # worker then finds no row and acks it harmlessly (see worker._process).
    event.listen(db, "after_commit", _publish, once=True)
