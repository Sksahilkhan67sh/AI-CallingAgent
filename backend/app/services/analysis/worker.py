"""The analysis worker -- Checkpoint 06 §7-8, §20-22.

Processing contract for one job:

  read -> load CallAnalysis -> idempotent claim (PostgreSQL is
  authoritative, §6) -> load durable conversation data -> transcript
  preparation -> LLM analysis -> validation -> deterministic scoring ->
  persist -> emit event/audit -> ack

Nothing is acked before the outcome is durably persisted (mirrors
app/services/queue/dialer_worker.py's own contract: flushed within the
open transaction, committed by the caller's loop immediately after --
same accepted convention as CP03, not a stricter one introduced here).

A transiently-failed job (LLM timeout/provider error/malformed output,
still under the attempt cap) is deliberately left UNACKED: this gives a
natural backoff window before AnalysisQueue.reclaim_stale() redelivers
it (bounded by `analysis_reclaim_idle_ms`), reusing the existing
crash-recovery mechanism as the retry-backoff mechanism too (§21-22),
rather than building a second delay-queue. Once the attempt cap is
reached, the analysis is marked terminally FAILED and the job IS acked
-- it must never be retried indefinitely (§21) and analysis failure
must never touch call/retry/campaign state (§0).
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import AnalysisIntent, AnalysisNextAction, AnalysisSentiment, InterestStatus
from app.services.analysis.job import AnalysisJob
from app.services.analysis.llm.base import (
    AnalysisLLM,
    AnalysisLLMProviderError,
    AnalysisLLMTimeoutError,
    AnalysisLLMValidationError,
)
from app.services.analysis.llm.schemas import AnalysisResult
from app.services.analysis.queue import AnalysisQueue
from app.services.analysis.repository import CallAnalysisRepository
from app.services.analysis.scoring import compute_lead_score
from app.services.analysis.transcript import (
    PreparedTranscript,
    load_conversation_session,
    prepare_transcript,
)
from app.services.audit_service import record_audit_event

logger = logging.getLogger("analysis_worker")

_ACTOR = "analysis-worker"

# Bounded in-process retry for a single delivery (mirrors
# ConversationOrchestrator.MAX_LLM_RETRIES) -- distinct from the
# cross-delivery attempt cap (`analysis_max_attempts`), which bounds
# how many separate queue deliveries this analysis may consume.
MAX_INPROCESS_LLM_RETRIES = 1


class AnalysisJobOutcome:
    COMPLETED = "completed"
    FAILED_RETRIABLE = "failed_retriable"  # left unacked -- backoff redelivery
    FAILED_TERMINAL = "failed_terminal"  # acked -- attempt cap reached
    ALREADY_PROCESSED = "already_processed"  # acked -- idempotent no-op
    NOT_ELIGIBLE = "not_eligible"  # acked -- data problem, will never succeed
    NO_JOB = "no_job"


def process_one_analysis_job(
    db: Session,
    queue: AnalysisQueue,
    llm: AnalysisLLM,
    *,
    consumer_name: str,
    block_ms: int = 1000,
) -> str:
    read = queue.read_one(consumer_name, block_ms)
    if read is None:
        return AnalysisJobOutcome.NO_JOB

    message_id, job = read
    return process_claimed_job(db, queue, llm, message_id, job)


def process_claimed_job(
    db: Session, queue: AnalysisQueue, llm: AnalysisLLM, message_id: str, job: AnalysisJob
) -> str:
    """Processes one already-claimed delivery -- used both for a freshly
    read job (`process_one_analysis_job`) and for a job handed back by
    `AnalysisQueue.reclaim_stale` (a crashed worker's leftover, or one
    deliberately left unacked for the retry-backoff window, §21-22).
    Unlike CP03's app.worker (which calls reclaim_stale but never
    re-drives processing on what it returns -- unexercised by any CP03
    test), CP06's backoff design depends on reclaimed jobs actually
    being reprocessed, so app.analysis_worker calls this explicitly for
    each one.
    """
    analysis = db.get(CallAnalysis, uuid.UUID(job.analysis_id))
    if analysis is None:
        logger.warning("analysis_target_missing", extra={"job_id": job.job_id})
        queue.ack(message_id)
        return AnalysisJobOutcome.NOT_ELIGIBLE

    repo = CallAnalysisRepository(db)

    claimed = repo.claim_for_processing(analysis.id)
    if claimed is None:
        refreshed = repo.get_by_call_attempt(analysis.call_attempt_id)
        if refreshed is not None and refreshed.status.value == "completed":
            queue.ack(message_id)
            return AnalysisJobOutcome.ALREADY_PROCESSED
        if refreshed is not None and repo.is_exhausted(refreshed):
            queue.ack(message_id)
            return AnalysisJobOutcome.FAILED_TERMINAL
        # Currently owned by another live worker (defensive -- XAUTOCLAIM's
        # atomic ownership transfer makes this rare in practice). Leave
        # unacked so it can be reclaimed later rather than lost.
        return AnalysisJobOutcome.FAILED_RETRIABLE

    outcome = _run_analysis(db, repo, llm, claimed, job)
    if outcome in (
        AnalysisJobOutcome.COMPLETED,
        AnalysisJobOutcome.FAILED_TERMINAL,
        AnalysisJobOutcome.NOT_ELIGIBLE,
    ):
        queue.ack(message_id)
    return outcome


def _run_analysis(
    db: Session,
    repo: CallAnalysisRepository,
    llm: AnalysisLLM,
    analysis: CallAnalysis,
    job: AnalysisJob,
) -> str:
    call_attempt = db.get(CallAttempt, uuid.UUID(job.call_attempt_id))
    contact = db.get(Contact, uuid.UUID(job.contact_id))
    if call_attempt is None or contact is None:
        _fail_terminal(db, repo, analysis, error_code="missing_target", error_message="")
        return AnalysisJobOutcome.NOT_ELIGIBLE

    session = load_conversation_session(db, call_attempt.id)
    if session is None:
        _fail_terminal(
            db, repo, analysis, error_code="no_conversation_session", error_message=""
        )
        return AnalysisJobOutcome.NOT_ELIGIBLE

    transcript = prepare_transcript(db, session)

    if transcript.message_count == 0:
        _complete(
            db,
            repo,
            analysis,
            _empty_conversation_result(),
            transcript,
            model_provider=None,
            model_name=None,
        )
        return AnalysisJobOutcome.COMPLETED

    try:
        result = _analyze_with_retries(llm, transcript.lines)
    except (
        AnalysisLLMTimeoutError,
        AnalysisLLMProviderError,
        AnalysisLLMValidationError,
    ) as exc:
        return _handle_llm_failure(db, repo, analysis, exc)

    _complete(
        db,
        repo,
        analysis,
        result,
        transcript,
        model_provider="mock",
        model_name=type(llm).__name__,
    )
    return AnalysisJobOutcome.COMPLETED


def _analyze_with_retries(llm: AnalysisLLM, transcript_lines: list[str]) -> AnalysisResult:
    last_exc: Exception | None = None
    for attempt in range(MAX_INPROCESS_LLM_RETRIES + 1):
        try:
            return llm.analyze(transcript_lines, brand_name="")
        except AnalysisLLMTimeoutError as exc:
            logger.warning("analysis_llm_timeout", extra={"attempt": attempt})
            last_exc = exc
        except AnalysisLLMProviderError as exc:
            logger.warning("analysis_llm_provider_error", extra={"attempt": attempt})
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _handle_llm_failure(
    db: Session, repo: CallAnalysisRepository, analysis: CallAnalysis, exc: Exception
) -> str:
    error_code = type(exc).__name__
    repo.mark_failed(analysis, error_code=error_code, error_message=str(exc)[:2000])

    if repo.is_exhausted(analysis):
        db.add(
            CallEvent(
                call_attempt_id=analysis.call_attempt_id,
                event_type="ANALYSIS_FAILED",
                payload={"analysis_id": str(analysis.id), "error_code": error_code},
            )
        )
        record_audit_event(
            db,
            actor=_ACTOR,
            action="analysis.failed",
            entity_type="call_analysis",
            entity_id=analysis.id,
            metadata={"error_code": error_code, "attempt_count": analysis.attempt_count},
        )
        db.flush()
        return AnalysisJobOutcome.FAILED_TERMINAL

    db.add(
        CallEvent(
            call_attempt_id=analysis.call_attempt_id,
            event_type="ANALYSIS_RETRY_SCHEDULED",
            payload={"analysis_id": str(analysis.id), "error_code": error_code},
        )
    )
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.retry_scheduled",
        entity_type="call_analysis",
        entity_id=analysis.id,
        metadata={"error_code": error_code, "attempt_count": analysis.attempt_count},
    )
    db.flush()
    return AnalysisJobOutcome.FAILED_RETRIABLE


def _fail_terminal(
    db: Session,
    repo: CallAnalysisRepository,
    analysis: CallAnalysis,
    *,
    error_code: str,
    error_message: str,
) -> None:
    repo.mark_failed(analysis, error_code=error_code, error_message=error_message)
    db.add(
        CallEvent(
            call_attempt_id=analysis.call_attempt_id,
            event_type="ANALYSIS_FAILED",
            payload={"analysis_id": str(analysis.id), "error_code": error_code},
        )
    )
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.failed",
        entity_type="call_analysis",
        entity_id=analysis.id,
        metadata={"error_code": error_code},
    )
    db.flush()


def _complete(
    db: Session,
    repo: CallAnalysisRepository,
    analysis: CallAnalysis,
    result: AnalysisResult,
    transcript: PreparedTranscript,
    *,
    model_provider: str | None,
    model_name: str | None,
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    lead_score = compute_lead_score(result.scoring_signals)

    repo.mark_completed(
        analysis,
        summary=result.summary,
        intent=result.intent,
        interest_status=result.interest_status,
        sentiment=result.sentiment,
        next_action=result.next_action,
        feedback=result.feedback,
        key_facts=result.key_facts,
        objections=result.objections,
        customer_needs=result.customer_needs,
        language=result.language,
        lead_score=lead_score,
        model_provider=model_provider,
        model_name=model_name,
        prompt_version=settings.analysis_prompt_version,
        analysis_version=settings.analysis_version,
        input_message_count=transcript.message_count,
        input_duration_seconds=transcript.duration_seconds,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    db.add(
        CallEvent(
            call_attempt_id=analysis.call_attempt_id,
            event_type="ANALYSIS_COMPLETED",
            payload={"analysis_id": str(analysis.id), "lead_score": lead_score},
        )
    )
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.completed",
        entity_type="call_analysis",
        entity_id=analysis.id,
        metadata={"lead_score": lead_score},
    )
    db.flush()


def _empty_conversation_result() -> AnalysisResult:
    from app.services.analysis.llm.schemas import ScoringSignals

    return AnalysisResult(
        summary="Call connected but no conversation content was captured.",
        intent=AnalysisIntent.UNCLEAR,
        interest_status=InterestStatus.UNKNOWN,
        sentiment=AnalysisSentiment.UNKNOWN,
        next_action=AnalysisNextAction.MANUAL_REVIEW,
        feedback=None,
        language="en",
        scoring_signals=ScoringSignals(),
    )
