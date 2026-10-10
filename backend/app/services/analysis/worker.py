"""The analysis worker -- Checkpoint 06 §7-8, §20-22; reliability rewrite by CP14B.

Processing contract for one job (CP14B §6):

  CLAIM -> COMMIT CLAIM -> LOAD/VALIDATE INPUT -> COMMIT -> ANALYZE (no DB transaction open)
        -> FENCED PERSIST (result | retry state | skip | fail) -> COMMIT -> ACK

  * The claim (a fresh fencing token + a lease) is committed BEFORE any slow work, and the
    session holds no transaction or row lock while the provider is called.
  * The job is acked only AFTER the corresponding durable state has committed. If a write or
    commit fails, the exception propagates, the job is NOT acked, and the row is recovered
    by lease expiry (sweeper republish / stream reclaim).
  * Acking is always safe once PostgreSQL holds the truth, because the sweeper rediscovers
    every unfinished row. So a job that cannot be claimed (finished, live lease elsewhere,
    not yet due, budget-deferred) is acked, never left to spin.
  * Exactly-once provider execution is NOT guaranteed: this is at-least-once with idempotent,
    fenced persistence. Two concurrent workers may both fetch; at most one result commits.
  * Retry ownership: the worker alone decides retry/skip/fail and records `next_attempt_at`;
    the sweeper alone republishes due work. Adapters make one request and never retry.
  * Analysis failure never touches call/retry/campaign/suppression state (§0).
"""

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.services.analysis import failure
from app.services.analysis.admission import is_eligible
from app.services.analysis.failure import FailureKind
from app.services.analysis.job import AnalysisJob
from app.services.analysis.llm.base import AnalysisContext, AnalysisLLM
from app.services.analysis.llm.schemas import AnalysisResult
from app.services.analysis.queue import AnalysisQueue
from app.services.analysis.repository import (
    TERMINAL_STATUSES,
    CallAnalysisRepository,
    utcnow,
)
from app.services.analysis.scoring import compute_lead_score
from app.services.analysis.transcript import (
    PreparedTranscript,
    load_conversation_session,
    prepare_transcript,
)
from app.services.audit_service import record_audit_event

logger = logging.getLogger("analysis_worker")

_ACTOR = "analysis-worker"


class AnalysisJobOutcome:
    COMPLETED = "completed"
    FAILED_RETRIABLE = "failed_retriable"  # retry state durable, job acked; sweeper republishes
    FAILED_TERMINAL = "failed_terminal"  # permanent / attempts exhausted
    SKIPPED = "skipped"  # nothing analyzable / QA never ran (terminal)
    DEFERRED = "deferred"  # daily budget gate; stays pending, rechecked later
    STALE = "stale"  # our claim was superseded; we wrote nothing
    ALREADY_PROCESSED = "already_processed"  # terminal already
    NOT_CLAIMABLE = "not_claimable"  # live lease elsewhere / not yet due
    NOT_ELIGIBLE = "not_eligible"  # target row missing
    NO_JOB = "no_job"


@dataclass
class _Input:
    transcript: PreparedTranscript
    context: AnalysisContext
    created_at: datetime


@dataclass
class _Skip:
    error_code: str
    release_budget: bool = False


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
    db: Session,
    queue: AnalysisQueue,
    llm: AnalysisLLM,
    message_id: str,
    job: AnalysisJob,
    *,
    clock: Callable[[], datetime] = utcnow,
    rng: Callable[[], float] = random.random,
) -> str:
    """Processes one delivered job (fresh read or a stream-reclaimed leftover)."""
    settings = get_settings()
    repo = CallAnalysisRepository(db)
    analysis_id = uuid.UUID(job.analysis_id)

    # 1. CLAIM + COMMIT CLAIM
    claim = repo.claim_for_processing(
        analysis_id,
        now=clock(),
        lease_seconds=settings.analysis_lease_seconds,
        max_attempts=settings.analysis_max_attempts,
    )
    db.commit()
    if claim.analysis is None or claim.claim_token is None:
        return _handle_unclaimed(
            db, repo, queue, message_id, analysis_id, claim.deferred_reason, clock
        )
    token = claim.claim_token
    attempt_count = claim.analysis.attempt_count

    # 2. LOAD / VALIDATE INPUT (short read transaction, ended before any slow work)
    loaded = _load_input(db, analysis_id)
    db.commit()
    if isinstance(loaded, _Skip):
        return _persist_skip(db, repo, queue, message_id, analysis_id, token, loaded, clock)
    if not repo.renew_lease(
        analysis_id, token, now=clock(), lease_seconds=settings.analysis_lease_seconds
    ):
        db.commit()
        queue.ack(message_id)
        return AnalysisJobOutcome.STALE
    db.commit()

    # 3. ANALYZE -- no transaction, no row lock held here.
    try:
        result = llm.analyze(loaded.transcript.lines, brand_name="", context=loaded.context)
    except Exception as exc:  # classified below; never swallowed
        kind, code, retry_after = failure.classify(exc)
        return _persist_failure(
            db,
            repo,
            queue,
            message_id,
            analysis_id,
            token,
            loaded,
            attempt_count,
            kind,
            code,
            retry_after,
            clock,
            rng,
        )

    # 4. FENCED PERSIST + COMMIT, then 5. ACK
    return _persist_success(
        db, repo, queue, message_id, analysis_id, token, result, loaded, llm, clock
    )


# -- unclaimed ---------------------------------------------------------------------------


def _handle_unclaimed(
    db: Session,
    repo: CallAnalysisRepository,
    queue: AnalysisQueue,
    message_id: str,
    analysis_id: uuid.UUID,
    deferred_reason: str | None,
    clock: Callable[[], datetime],
) -> str:
    settings = get_settings()
    if deferred_reason is not None:
        logger.info("analysis_deferred", extra={"reason": deferred_reason})
        queue.ack(message_id)  # durable: next_attempt_at set; sweeper rechecks at low frequency
        return AnalysisJobOutcome.DEFERRED

    row = db.get(CallAnalysis, analysis_id)
    if row is None:
        db.commit()
        logger.warning("analysis_target_missing")
        queue.ack(message_id)
        return AnalysisJobOutcome.NOT_ELIGIBLE
    status = row.status
    exhausted = row.attempt_count >= settings.analysis_max_attempts
    db.commit()
    if status in TERMINAL_STATUSES:
        queue.ack(message_id)
        return AnalysisJobOutcome.ALREADY_PROCESSED
    if exhausted:
        # Attempts used up and no live owner: make it terminal (also done by the sweeper).
        repo.finalize_exhausted(
            now=clock(), max_attempts=settings.analysis_max_attempts, analysis_id=analysis_id
        )
        db.commit()
        queue.ack(message_id)
        return AnalysisJobOutcome.FAILED_TERMINAL
    # Live lease elsewhere, or not yet due: the sweeper republishes when it matters.
    queue.ack(message_id)
    return AnalysisJobOutcome.NOT_CLAIMABLE


# -- input -------------------------------------------------------------------------------


def _load_input(db: Session, analysis_id: uuid.UUID) -> _Input | _Skip:
    settings = get_settings()
    analysis = db.get(CallAnalysis, analysis_id)
    assert analysis is not None
    created_at = analysis.created_at
    attempt = db.get(CallAttempt, analysis.call_attempt_id)
    contact = db.get(Contact, analysis.contact_id)
    # Never trust the stored identifiers blindly: re-verify the persisted relationships.
    if (
        attempt is None
        or contact is None
        or attempt.contact_id != contact.id
        or contact.campaign_id != analysis.campaign_id
    ):
        return _Skip(failure.ERR_BAD_RELATIONSHIP, release_budget=True)
    if not is_eligible(attempt, contact):
        return _Skip(failure.ERR_NOT_ELIGIBLE, release_budget=True)

    session = load_conversation_session(db, attempt.id)
    if session is None:
        return _Skip(failure.ERR_NO_SESSION, release_budget=True)
    if analysis.conversation_session_id not in (None, session.id):
        return _Skip(failure.ERR_BAD_RELATIONSHIP, release_budget=True)
    transcript = prepare_transcript(db, session)
    if not transcript.lines:
        # An empty/unavailable conversation is NOT a successful zero-interest result.
        return _Skip(failure.ERR_EMPTY_CONVERSATION, release_budget=True)

    run_id: int | None = None
    if settings.analysis_llm_provider == "dograh_qa":
        try:
            run_id = int(attempt.provider_call_id) if attempt.provider_call_id else None
        except ValueError:
            run_id = None
    context = AnalysisContext(
        dograh_workflow_id=attempt.dograh_workflow_id or settings.dograh_workflow_id,
        dograh_run_id=run_id,
        call_duration_seconds=attempt.duration_seconds,
    )
    if settings.analysis_llm_provider == "dograh_qa" and (
        context.dograh_run_id is None or context.dograh_workflow_id is None
    ):
        # No identifiers => QA output can never be fetched; do not burn retries on it.
        return _Skip(failure.ERR_MISSING_RUN_ID, release_budget=True)
    return _Input(transcript=transcript, context=context, created_at=created_at)


# -- persistence -------------------------------------------------------------------------


def _event(db: Session, analysis_id: uuid.UUID, event_type: str, **payload: object) -> None:
    attempt_id = (
        db.query(CallAnalysis.call_attempt_id).filter(CallAnalysis.id == analysis_id).scalar()
    )
    db.add(
        CallEvent(
            call_attempt_id=attempt_id,
            event_type=event_type,
            payload={"analysis_id": str(analysis_id), **payload},
        )
    )


def _stale(db: Session, queue: AnalysisQueue, message_id: str) -> str:
    # The fenced write matched nothing: another claim owns / finished this row. Writing
    # nothing is the point. PostgreSQL holds the truth, so acking is safe.
    db.commit()
    queue.ack(message_id)
    logger.info("analysis_stale_claim_discarded")
    return AnalysisJobOutcome.STALE


def _persist_success(
    db: Session,
    repo: CallAnalysisRepository,
    queue: AnalysisQueue,
    message_id: str,
    analysis_id: uuid.UUID,
    token: uuid.UUID,
    result: AnalysisResult,
    loaded: _Input,
    llm: AnalysisLLM,
    clock: Callable[[], datetime],
) -> str:
    settings = get_settings()
    lead_score = compute_lead_score(result.scoring_signals)
    observed = getattr(llm, "last_observed_run_cost_usd", None)
    applied = repo.complete(
        analysis_id,
        token,
        now=clock(),
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
        model_provider=getattr(llm, "provider_name", "mock"),
        model_name=getattr(llm, "model_name", type(llm).__name__),
        prompt_version=settings.analysis_prompt_version,
        analysis_version=settings.analysis_version,
        input_message_count=loaded.transcript.message_count,
        input_duration_seconds=loaded.transcript.duration_seconds,
        truncated=loaded.transcript.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        **({"observed_run_cost_usd": observed} if observed is not None else {}),
    )
    if not applied:
        return _stale(db, queue, message_id)
    _event(db, analysis_id, "ANALYSIS_COMPLETED", lead_score=lead_score)
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.completed",
        entity_type="call_analysis",
        entity_id=analysis_id,
        metadata={"lead_score": lead_score},
    )
    db.commit()  # durable ...
    queue.ack(message_id)  # ... only then ack
    return AnalysisJobOutcome.COMPLETED


def _persist_skip(
    db: Session,
    repo: CallAnalysisRepository,
    queue: AnalysisQueue,
    message_id: str,
    analysis_id: uuid.UUID,
    token: uuid.UUID,
    skip: _Skip,
    clock: Callable[[], datetime],
) -> str:
    applied = repo.skip(
        analysis_id,
        token,
        now=clock(),
        error_code=skip.error_code,
        release_budget=skip.release_budget,
    )
    if not applied:
        return _stale(db, queue, message_id)
    _event(db, analysis_id, "ANALYSIS_SKIPPED", error_code=skip.error_code)
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.skipped",
        entity_type="call_analysis",
        entity_id=analysis_id,
        metadata={"error_code": skip.error_code},
    )
    db.commit()
    queue.ack(message_id)
    return AnalysisJobOutcome.SKIPPED


def _persist_failure(
    db: Session,
    repo: CallAnalysisRepository,
    queue: AnalysisQueue,
    message_id: str,
    analysis_id: uuid.UUID,
    token: uuid.UUID,
    loaded: _Input,
    attempt_count: int,
    kind: FailureKind,
    code: str,
    retry_after: int | None,
    clock: Callable[[], datetime],
    rng: Callable[[], float],
) -> str:
    settings = get_settings()
    now = clock()

    if kind == FailureKind.QA_UNAVAILABLE:
        return _persist_skip(
            db,
            repo,
            queue,
            message_id,
            analysis_id,
            token,
            _Skip(failure.ERR_QA_UNAVAILABLE, release_budget=True),
            clock,
        )

    if kind == FailureKind.QA_NOT_READY:
        elapsed = (now - loaded.created_at).total_seconds()
        if elapsed >= settings.analysis_qa_ready_deadline_seconds:
            # "Not ready yet" has become "never produced": QA was sampled out, disabled, or
            # failed inside Dograh (indistinguishable from the API -- documented limitation).
            return _persist_skip(
                db,
                repo,
                queue,
                message_id,
                analysis_id,
                token,
                _Skip(failure.ERR_QA_NOT_PRODUCED, release_budget=True),
                clock,
            )
        delay = failure.poll_delay_seconds(
            elapsed,
            base=settings.analysis_retry_base_seconds,
            cap=settings.analysis_retry_max_seconds,
            rng=rng,
        )
        return _persist_retry(
            db,
            repo,
            queue,
            message_id,
            analysis_id,
            token,
            code,
            now + timedelta(seconds=delay),
            refund=True,
        )

    if kind == FailureKind.PERMANENT or attempt_count >= settings.analysis_max_attempts:
        applied = repo.fail(analysis_id, token, now=now, error_code=code)
        if not applied:
            return _stale(db, queue, message_id)
        _event(db, analysis_id, "ANALYSIS_FAILED", error_code=code)
        record_audit_event(
            db,
            actor=_ACTOR,
            action="analysis.failed",
            entity_type="call_analysis",
            entity_id=analysis_id,
            metadata={"error_code": code, "attempt_count": attempt_count},
        )
        db.commit()
        queue.ack(message_id)
        return AnalysisJobOutcome.FAILED_TERMINAL

    delay = failure.backoff_seconds(
        attempt_count,
        base=settings.analysis_retry_base_seconds,
        cap=settings.analysis_retry_max_seconds,
        retry_after=retry_after,
        rng=rng,
    )
    return _persist_retry(
        db,
        repo,
        queue,
        message_id,
        analysis_id,
        token,
        code,
        now + timedelta(seconds=delay),
        refund=False,
    )


def _persist_retry(
    db: Session,
    repo: CallAnalysisRepository,
    queue: AnalysisQueue,
    message_id: str,
    analysis_id: uuid.UUID,
    token: uuid.UUID,
    code: str,
    next_attempt_at: datetime,
    *,
    refund: bool,
) -> str:
    applied = repo.schedule_retry(
        analysis_id,
        token,
        now=utcnow(),
        error_code=code,
        next_attempt_at=next_attempt_at,
        refund_attempt=refund,
    )
    if not applied:
        return _stale(db, queue, message_id)
    _event(db, analysis_id, "ANALYSIS_RETRY_SCHEDULED", error_code=code)
    record_audit_event(
        db,
        actor=_ACTOR,
        action="analysis.retry_scheduled",
        entity_type="call_analysis",
        entity_id=analysis_id,
        metadata={"error_code": code},
    )
    db.commit()  # retry state durable ...
    queue.ack(message_id)  # ... only then ack; the sweeper republishes at next_attempt_at
    return AnalysisJobOutcome.FAILED_RETRIABLE
