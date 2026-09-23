"""RecoveryManager -- the single owner of retry/reconnect decisions,
Checkpoint 05 §4, §7.

Two entry points, one decision engine: the ConversationOrchestrator
calls this on a mid-call disconnect; the dialer worker calls this on a
never-connected failure. Both funnel through `_decide` and
`_schedule`/`_terminalize` -- there is exactly one place that reads
RetryPolicy and decides retry vs terminal, matching "no duplicate retry
logic."
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CampaignStatus, ContactStatus
from app.models.retry_policy import RetryPolicy
from app.repositories.suppression_repository import SuppressionRepository
from app.services.analysis.admission import enqueue_call_analysis
from app.services.audit_service import record_audit_event
from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import RecoveryScheduler

logger = logging.getLogger("recovery")

_ACTOR = "recovery-manager"


@dataclass
class RecoveryDecision:
    should_retry: bool
    reason: str
    delay_seconds: int | None = None
    next_attempt_number: int | None = None


class RecoveryManager:
    def __init__(self, db: Session, scheduler: RecoveryScheduler) -> None:
        self.db = db
        self.scheduler = scheduler
        self.suppressions = SuppressionRepository(db)

    def handle_disconnect(
        self,
        call_attempt: CallAttempt,
        contact: Contact,
        campaign: Campaign,
        *,
        never_connected: bool,
        reason_key: str,
    ) -> RecoveryDecision:
        """§8: opt-out/suppression always wins, checked first and
        independent of the disconnect reason itself."""
        if contact.status == ContactStatus.CLOSED or self.suppressions.is_suppressed(
            contact.normalized_phone_number
        ):
            return self._terminalize(call_attempt, contact, "suppressed")

        if campaign.status == CampaignStatus.COMPLETED:
            return self._terminalize(call_attempt, contact, "campaign_completed")

        retry_policy = self.db.execute(
            select(RetryPolicy).where(RetryPolicy.campaign_id == campaign.id)
        ).scalar_one_or_none()

        decision = self._decide(call_attempt, retry_policy, never_connected, reason_key)

        if decision.should_retry:
            self._schedule(call_attempt, contact, campaign, decision)
        else:
            self._terminalize(call_attempt, contact, decision.reason)

        return decision

    # -- decision -------------------------------------------------------

    def _decide(
        self,
        call_attempt: CallAttempt,
        retry_policy: RetryPolicy | None,
        never_connected: bool,
        reason_key: str,
    ) -> RecoveryDecision:
        if retry_policy is None:
            # No policy configured for this campaign -- conservative
            # default: no retry, rather than guessing spacing/limits.
            return RecoveryDecision(should_retry=False, reason="no_retry_policy_configured")

        rules = (
            retry_policy.never_connected_rules if never_connected else retry_policy.mid_call_rules
        )
        if not rules.get(reason_key, False):
            return RecoveryDecision(
                should_retry=False, reason=f"reason_not_retryable:{reason_key}"
            )

        if call_attempt.attempt_number > retry_policy.max_retries:
            return RecoveryDecision(should_retry=False, reason="max_attempts_reached")

        spacing_index = call_attempt.attempt_number - 1
        if spacing_index >= len(retry_policy.retry_spacing_seconds):
            return RecoveryDecision(should_retry=False, reason="max_attempts_reached")

        delay_seconds = retry_policy.retry_spacing_seconds[spacing_index]
        return RecoveryDecision(
            should_retry=True,
            reason="scheduled",
            delay_seconds=delay_seconds,
            next_attempt_number=call_attempt.attempt_number + 1,
        )

    # -- effects ----------------------------------------------------

    def _schedule(
        self,
        call_attempt: CallAttempt,
        contact: Contact,
        campaign: Campaign,
        decision: RecoveryDecision,
    ) -> None:
        assert decision.delay_seconds is not None
        assert decision.next_attempt_number is not None

        job = RecoveryJob.new(
            attempt_id=call_attempt.id,
            contact_id=contact.id,
            campaign_id=campaign.id,
            attempt_number=decision.next_attempt_number,
        )
        due_at = datetime.now(UTC) + timedelta(seconds=decision.delay_seconds)
        self.scheduler.schedule(job, due_at)

        contact.status = ContactStatus.RETRY_SCHEDULED

        self._log_event(
            call_attempt,
            "RECOVERY_SCHEDULED",
            {
                "next_attempt_number": decision.next_attempt_number,
                "delay_seconds": decision.delay_seconds,
                "trace_id": job.trace_id,
            },
        )
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="recovery.scheduled",
            entity_type="call_attempt",
            entity_id=call_attempt.id,
            metadata={
                "next_attempt_number": decision.next_attempt_number,
                "delay_seconds": decision.delay_seconds,
            },
        )
        self.db.flush()

    def _terminalize(
        self, call_attempt: CallAttempt, contact: Contact, reason: str
    ) -> RecoveryDecision:
        self._log_event(call_attempt, "RECOVERY_TERMINALIZED", {"reason": reason})
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="recovery.terminalized",
            entity_type="call_attempt",
            entity_id=call_attempt.id,
            metadata={"reason": reason},
        )

        if reason != "suppressed":
            # a suppressed contact is already Closed -- don't relabel it
            contact.status = ContactStatus.COMPLETED_PARTIAL

        self.db.flush()

        # Checkpoint 06: same single admission entry point as the
        # orchestrator's graceful-ending path. Called unconditionally;
        # it self-filters on call_attempt.state (excludes
        # FailedToConnect -- no conversation occurred) and contact.status
        # (excludes Closed/suppressed).
        enqueue_call_analysis(self.db, call_attempt, contact)

        return RecoveryDecision(should_retry=False, reason=reason)

    def _log_event(self, call_attempt: CallAttempt, event_type: str, payload: dict) -> None:
        self.db.add(
            CallEvent(call_attempt_id=call_attempt.id, event_type=event_type, payload=payload)
        )
        self.db.flush()
