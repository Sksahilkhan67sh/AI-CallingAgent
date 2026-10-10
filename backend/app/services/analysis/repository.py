"""CallAnalysisRepository -- Checkpoint 06 §6 idempotency, CP14B lease + fencing.

PostgreSQL is authoritative for "was this call analyzed", never Redis.

Ownership model (CP14B):

  * `claim_for_processing` is ONE atomic conditional UPDATE: it moves a due PENDING /
    RETRY_WAIT row, or a PROCESSING row whose lease has expired, to PROCESSING with a FRESH
    `claim_token` (the fencing token), a lease, and attempt_count + 1. The caller COMMITS the
    claim before doing any slow work, so no row lock or transaction spans a provider request.
  * Every outcome write (`complete`, `schedule_retry`, `fail`, `skip`) is fenced:
    `WHERE id = :id AND claim_token = :mine AND status = 'processing'`. A worker whose lease
    expired -- and whose row was re-claimed or already finished -- matches zero rows and
    cannot overwrite the newer outcome. It learns that from the returned bool.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.enums import AnalysisStatus
from app.services.analysis import budget
from app.services.analysis.budget import BudgetDecision

TERMINAL_STATUSES = (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED, AnalysisStatus.SKIPPED)


def utcnow() -> datetime:
    return datetime.now(UTC)


class _NotClaimable(Exception):
    pass


class _Deferred(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ClaimResult:
    analysis: CallAnalysis | None
    claim_token: uuid.UUID | None = None
    deferred_reason: str | None = None


class CallAnalysisRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # -- admission ---------------------------------------------------------------------

    def get_by_call_attempt(self, call_attempt_id: uuid.UUID) -> CallAnalysis | None:
        return (
            self.db.query(CallAnalysis)
            .filter(CallAnalysis.call_attempt_id == call_attempt_id)
            .one_or_none()
        )

    def get_or_create_pending(
        self,
        *,
        call_attempt_id: uuid.UUID,
        contact_id: uuid.UUID,
        campaign_id: uuid.UUID,
        conversation_session_id: uuid.UUID | None,
        last_enqueued_at: datetime | None = None,
        next_attempt_at: datetime | None = None,
    ) -> tuple[CallAnalysis, bool]:
        """Returns (analysis, created). Safe under concurrent/duplicate admission: an existing
        row (any status) is returned unchanged -- creation never overwrites an in-flight or
        completed analysis. The unique constraint is the real decider."""
        existing = self.get_by_call_attempt(call_attempt_id)
        if existing is not None:
            return existing, False

        analysis = CallAnalysis(
            call_attempt_id=call_attempt_id,
            contact_id=contact_id,
            campaign_id=campaign_id,
            conversation_session_id=conversation_session_id,
            status=AnalysisStatus.PENDING,
            last_enqueued_at=last_enqueued_at,
            next_attempt_at=next_attempt_at,
        )
        self.db.add(analysis)
        try:
            with self.db.begin_nested():  # SAVEPOINT -- only this insert unwinds on conflict
                self.db.flush()
        except IntegrityError:
            existing = self.get_by_call_attempt(call_attempt_id)
            assert existing is not None
            return existing, False
        return analysis, True

    # -- claim -------------------------------------------------------------------------

    def claim_for_processing(
        self,
        analysis_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimResult:
        """Atomically claim a due analysis (see module docstring). Returns the claim, or a
        result with analysis=None when it is not claimable (already finished, owned by a live
        lease, not yet due, attempts exhausted) or the daily budget deferred it.

        The budget reservation and the claim share one SAVEPOINT: a denied reservation
        un-claims the row, so a deferral never consumes an attempt or leaves a lease."""
        token = uuid.uuid4()
        try:
            with self.db.begin_nested():
                claimed_id = self.db.execute(
                    update(CallAnalysis)
                    .where(
                        CallAnalysis.id == analysis_id,
                        CallAnalysis.attempt_count < max_attempts,
                        or_(
                            and_(
                                CallAnalysis.status == AnalysisStatus.PENDING,
                                or_(
                                    CallAnalysis.next_attempt_at.is_(None),
                                    CallAnalysis.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                CallAnalysis.status == AnalysisStatus.RETRY_WAIT,
                                CallAnalysis.next_attempt_at <= now,
                            ),
                            and_(
                                CallAnalysis.status == AnalysisStatus.PROCESSING,
                                CallAnalysis.lease_expires_at < now,
                            ),
                        ),
                    )
                    .values(
                        status=AnalysisStatus.PROCESSING,
                        attempt_count=CallAnalysis.attempt_count + 1,
                        claim_token=token,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        processing_started_at=now,
                        next_attempt_at=None,
                    )
                    .returning(CallAnalysis.id)
                ).scalar_one_or_none()
                if claimed_id is None:
                    raise _NotClaimable
                analysis = self.db.get(CallAnalysis, claimed_id, populate_existing=True)
                assert analysis is not None
                decision = budget.reserve(self.db, analysis, now)
                if decision in (BudgetDecision.NOT_CONFIGURED, BudgetDecision.CAP_REACHED):
                    raise _Deferred(decision.value)
        except _NotClaimable:
            return ClaimResult(analysis=None)
        except _Deferred as deferred:
            self._defer(analysis_id, deferred.reason, now)
            return ClaimResult(analysis=None, deferred_reason=deferred.reason)
        return ClaimResult(analysis=analysis, claim_token=token)

    def _defer(self, analysis_id: uuid.UUID, reason: str, now: datetime) -> None:
        """Budget deferral: the row stays claimable-later (PENDING/RETRY_WAIT unchanged), is
        rechecked at a LOW frequency (never a hot loop), and records why."""
        from app.core.config import get_settings

        self.db.execute(
            update(CallAnalysis)
            .where(
                CallAnalysis.id == analysis_id,
                CallAnalysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RETRY_WAIT]),
            )
            .values(
                next_attempt_at=now
                + timedelta(seconds=get_settings().analysis_budget_recheck_seconds),
                error_code=reason,
                error_message=None,
            )
        )
        self.db.flush()

    # -- fenced writes -----------------------------------------------------------------

    def _fenced(self, analysis_id: uuid.UUID, token: uuid.UUID, **values: Any) -> bool:
        result = self.db.execute(
            update(CallAnalysis)
            .where(
                CallAnalysis.id == analysis_id,
                CallAnalysis.claim_token == token,
                CallAnalysis.status == AnalysisStatus.PROCESSING,
            )
            .values(**values)
            .returning(CallAnalysis.id)
        )
        applied = result.scalar_one_or_none() is not None
        self.db.flush()
        if applied:
            self.db.expire_all()
        return applied

    def renew_lease(
        self, analysis_id: uuid.UUID, token: uuid.UUID, *, now: datetime, lease_seconds: int
    ) -> bool:
        return self._fenced(
            analysis_id, token, lease_expires_at=now + timedelta(seconds=lease_seconds)
        )

    def complete(
        self, analysis_id: uuid.UUID, token: uuid.UUID, *, now: datetime, **fields: object
    ) -> bool:
        return self._fenced(
            analysis_id,
            token,
            status=AnalysisStatus.COMPLETED,
            completed_at=now,
            error_code=None,
            error_message=None,
            claim_token=None,
            lease_expires_at=None,
            next_attempt_at=None,
            **fields,
        )

    def schedule_retry(
        self,
        analysis_id: uuid.UUID,
        token: uuid.UUID,
        *,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime,
        refund_attempt: bool = False,
    ) -> bool:
        extra: dict[str, Any] = {}
        if refund_attempt:
            extra["attempt_count"] = CallAnalysis.attempt_count - 1
        return self._fenced(
            analysis_id,
            token,
            status=AnalysisStatus.RETRY_WAIT,
            failed_at=now,
            error_code=error_code,
            error_message=None,
            next_attempt_at=next_attempt_at,
            claim_token=None,
            lease_expires_at=None,
            **extra,
        )

    def fail(
        self, analysis_id: uuid.UUID, token: uuid.UUID, *, now: datetime, error_code: str
    ) -> bool:
        return self._fenced(
            analysis_id,
            token,
            status=AnalysisStatus.FAILED,
            failed_at=now,
            error_code=error_code,
            error_message=None,
            claim_token=None,
            lease_expires_at=None,
            next_attempt_at=None,
        )

    def skip(
        self,
        analysis_id: uuid.UUID,
        token: uuid.UUID,
        *,
        now: datetime,
        error_code: str,
        release_budget: bool = False,
    ) -> bool:
        before = self.db.get(CallAnalysis, analysis_id)
        budget_day = before.budget_day if before is not None else None
        reserved: Decimal | None = before.reserved_cost if before is not None else None
        applied = self._fenced(
            analysis_id,
            token,
            status=AnalysisStatus.SKIPPED,
            failed_at=now,
            error_code=error_code,
            error_message=None,
            claim_token=None,
            lease_expires_at=None,
            next_attempt_at=None,
            **({"reserved_cost": Decimal(0)} if release_budget and reserved else {}),
        )
        if applied and release_budget:
            budget.release(self.db, budget_day, reserved)
        return applied

    def is_exhausted(self, analysis: CallAnalysis, max_attempts: int) -> bool:
        return analysis.attempt_count >= max_attempts

    def finalize_exhausted(
        self,
        *,
        now: datetime,
        max_attempts: int,
        analysis_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> int:
        """Terminal-fail rows that can no longer be claimed: attempts used up AND (no live
        owner). Covers a worker that crashed on its LAST attempt -- nothing else would ever
        move that row. Bounded, idempotent, concurrency-safe (SKIP LOCKED). Returns count."""
        from app.services.analysis.failure import ERR_LEASE_EXPIRED

        stuck = (
            select(CallAnalysis.id)
            .where(
                CallAnalysis.attempt_count >= max_attempts,
                or_(
                    CallAnalysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.RETRY_WAIT]),
                    and_(
                        CallAnalysis.status == AnalysisStatus.PROCESSING,
                        CallAnalysis.lease_expires_at < now,
                    ),
                ),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if analysis_id is not None:
            stuck = stuck.where(CallAnalysis.id == analysis_id)
        ids = list(self.db.execute(stuck).scalars())
        if not ids:
            return 0
        self.db.execute(
            update(CallAnalysis)
            .where(CallAnalysis.id.in_(ids))
            .values(
                status=AnalysisStatus.FAILED,
                failed_at=now,
                error_code=ERR_LEASE_EXPIRED,
                error_message=None,
                claim_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
            )
        )
        self.db.flush()
        self.db.expire_all()
        return len(ids)
