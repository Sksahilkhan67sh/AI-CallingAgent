"""CallAnalysisRepository -- Checkpoint 06 §6 idempotency.

PostgreSQL is authoritative for "was this call analyzed", never Redis
(§6). `get_or_create_pending` is the admission-time entry point (safe
to call from multiple terminal-state paths, backstopped by the
`uq_call_analysis_call_attempt` unique constraint). `claim_for_processing`
is the worker-time entry point: an atomic conditional UPDATE is the
claim, exactly like CP03's CallAttemptRepository.get_or_create and
CP05's RecoveryScheduler.claim use a single atomic operation as the
race-decider between concurrent workers.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.enums import AnalysisStatus


class CallAnalysisRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

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
    ) -> tuple[CallAnalysis, bool]:
        """Returns (analysis, created). Safe under concurrent/duplicate
        admission: if a row already exists (any status), it is returned
        unchanged -- creation never overwrites an in-flight or completed
        analysis."""
        existing = self.get_by_call_attempt(call_attempt_id)
        if existing is not None:
            return existing, False

        analysis = CallAnalysis(
            call_attempt_id=call_attempt_id,
            contact_id=contact_id,
            campaign_id=campaign_id,
            conversation_session_id=conversation_session_id,
            status=AnalysisStatus.PENDING,
        )
        self.db.add(analysis)
        try:
            with self.db.begin_nested():  # SAVEPOINT -- only this insert unwinds on conflict
                self.db.flush()
        except IntegrityError:
            # Lost a race with another admission path/duplicate delivery
            # between the SELECT above and this INSERT -- the unique
            # constraint is the real decider (§6, §9).
            existing = self.get_by_call_attempt(call_attempt_id)
            assert existing is not None
            return existing, False
        return analysis, True

    def claim_for_processing(self, analysis_id: uuid.UUID) -> CallAnalysis | None:
        """Atomically transition PENDING or FAILED (retriable, under the
        attempt cap) -> PROCESSING. Returns None if another worker
        already claimed it, if it's already COMPLETED (idempotent no-op,
        §6), or if the attempt cap was reached (terminal FAILED, §21)."""
        from app.core.config import get_settings

        max_attempts = get_settings().analysis_max_attempts

        result = self.db.execute(
            update(CallAnalysis)
            .where(
                CallAnalysis.id == analysis_id,
                CallAnalysis.status.in_([AnalysisStatus.PENDING, AnalysisStatus.FAILED]),
                CallAnalysis.attempt_count < max_attempts,
            )
            .values(
                status=AnalysisStatus.PROCESSING,
                attempt_count=CallAnalysis.attempt_count + 1,
                processing_started_at=datetime.now(UTC),
            )
            .returning(CallAnalysis.id)
        )
        claimed_id = result.scalar_one_or_none()
        self.db.flush()
        if claimed_id is None:
            return None
        return self.db.get(CallAnalysis, claimed_id)

    def mark_completed(self, analysis: CallAnalysis, **fields: object) -> None:
        analysis.status = AnalysisStatus.COMPLETED
        analysis.completed_at = datetime.now(UTC)
        analysis.error_code = None
        analysis.error_message = None
        for key, value in fields.items():
            setattr(analysis, key, value)
        self.db.flush()

    def mark_failed(
        self, analysis: CallAnalysis, *, error_code: str, error_message: str
    ) -> None:
        """Interim failure (retriable, if attempt_count is still under
        the cap) or terminal failure (attempt cap reached) -- the caller
        decides which by whether it re-enqueues; either way the row
        itself just reflects "not currently succeeded" (§20-21)."""
        analysis.status = AnalysisStatus.FAILED
        analysis.failed_at = datetime.now(UTC)
        analysis.error_code = error_code
        analysis.error_message = error_message
        self.db.flush()

    def is_exhausted(self, analysis: CallAnalysis) -> bool:
        from app.core.config import get_settings

        return analysis.attempt_count >= get_settings().analysis_max_attempts
