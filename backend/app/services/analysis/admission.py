"""Analysis admission -- Checkpoint 06 §3, §9. The single authoritative
integration point that decides whether a just-terminalized CallAttempt
is eligible for post-call analysis, and if so, admits it.

Eligibility mirrors the repository's own terminal-state model exactly
(Core-Production-Logic.md §26-27) rather than inventing a competing
one:

  - The attempt must have reached Connected -- `call_attempt.state` is
    ENDED_NORMALLY or DROPPED_MID_CALL, never FAILED_TO_CONNECT. A call
    that never connected has no conversation to analyze.
  - The Contact's resulting terminal status must be COMPLETED or
    COMPLETED_PARTIAL, never CLOSED. Closed covers both "never
    connected, retries exhausted" (already excluded above) and
    "opted out during the call" -- an opted-out contact is
    intentionally excluded from analysis too (Core-Production-Logic.md
    §26: Closed produces "NO analysis, NO Final Output").

This function is called unconditionally from both existing
terminal-transition call sites --
ConversationOrchestrator._end_conversation() and
RecoveryManager._terminalize() -- rather than each site special-casing
its own eligibility check, so there is exactly one place this decision
is made (no duplicate/competing eligibility logic across the two call
paths). It never touches retry/campaign/suppression state itself --
admission only ever enqueues (§0: "The analysis pipeline MUST NOT
control calling/retry infrastructure directly").
"""

import logging

from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.job import AnalysisJob
from app.services.analysis.repository import CallAnalysisRepository
from app.services.analysis.transcript import load_conversation_session
from app.services.audit_service import record_audit_event

logger = logging.getLogger("analysis.admission")

_ACTOR = "analysis-admission"

_ELIGIBLE_ATTEMPT_STATES = (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
_ELIGIBLE_CONTACT_STATUSES = (ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL)


def enqueue_call_analysis(db: Session, call_attempt: CallAttempt, contact: Contact) -> None:
    if call_attempt.state not in _ELIGIBLE_ATTEMPT_STATES:
        return
    if contact.status not in _ELIGIBLE_CONTACT_STATUSES:
        return

    session = load_conversation_session(db, call_attempt.id)

    analysis, created = CallAnalysisRepository(db).get_or_create_pending(
        call_attempt_id=call_attempt.id,
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        conversation_session_id=session.id if session is not None else None,
    )
    if not created:
        # Already admitted by an earlier call to this function (duplicate
        # terminal-state path, duplicate delivery) -- idempotent no-op,
        # per §6/§9. Do not enqueue a second job for it.
        return

    job = AnalysisJob.new(
        analysis_id=analysis.id,
        call_attempt_id=call_attempt.id,
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        conversation_session_id=session.id if session is not None else None,
    )
    get_analysis_queue().enqueue(job)

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
