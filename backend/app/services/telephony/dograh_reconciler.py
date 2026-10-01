"""Dograh reconciliation -- Checkpoint 09.

Dograh does not retry failed webhooks by default and has no mid-call event,
so three situations would otherwise leave a call stuck forever:

  1. AMBIGUOUS trigger -- our request timed out / got a 5xx, so we do not
     know whether a run exists (attempt has `provider="dograh"` but no
     run id). We look for a run carrying our `call_attempt_id` in its
     initial_context. Found -> adopt it. Not found after the grace period
     -> conclude no call was placed and hand the failure to RecoveryManager.
     No second trigger is ever sent before this lookup has been made.
  2. LOST webhook -- run id known, no completion arrived. We read the run;
     if Dograh reports it completed we apply the completion exactly as the
     webhook would have.
  3. LOST run -- Dograh still reports it in progress long after any call
     could last: terminalized through RecoveryManager.

Retry decisions are never made here; failures go to RecoveryManager.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.services import call_state
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager
from app.services.telephony.dograh_client import DograhApiError, DograhClient, DograhRun
from app.services.telephony.dograh_webhook_service import (
    Completion,
    admit_analysis,
    apply_completion,
)

logger = logging.getLogger("dograh_reconciler")

_SOURCE = "dograh_reconciler"


def _completion_from_run(run: DograhRun) -> Completion:
    ctx = run.gathered_context
    raw_duration = run.cost_info.get("call_duration_seconds")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return Completion(
        run_id=str(run.run_id),
        call_status=str(ctx["call_status"]) if ctx.get("call_status") else None,
        disposition=str(ctx.get("mapped_call_disposition") or ctx.get("call_disposition") or "")
        or None,
        duration_seconds=duration,
        transcript_url=run.transcript_url,
    )


def _fail_without_run(
    db: Session, attempt: CallAttempt, contact: Contact, campaign: Campaign
) -> None:
    db.add(CallEvent(call_attempt_id=attempt.id, event_type="RECONCILED_NO_RUN", payload={}))
    call_state.transition(
        db,
        attempt,
        CallAttemptState.FAILED_TO_CONNECT,
        reason="reconciled_no_run",
        source=_SOURCE,
    )
    attempt.connection_failure_reason = NeverConnectedFailureReason.PROVIDER_ERROR
    attempt.ended_at = datetime.now(UTC)
    db.flush()
    metrics.incr(metrics.CALLS_FAILED)
    RecoveryManager(db, get_recovery_scheduler()).handle_disconnect(
        attempt, contact, campaign, never_connected=True, reason_key="provider_error"
    )


def _reconcile_one(db: Session, client: DograhClient, attempt: CallAttempt, now: datetime) -> str:
    contact = db.get(Contact, attempt.contact_id)
    campaign = db.get(Campaign, contact.campaign_id) if contact is not None else None
    if contact is None or campaign is None:
        return "skipped_missing_context"
    settings = get_settings()

    if attempt.provider_call_id is None:
        run = client.find_run_by_attempt(
            call_attempt_id=str(attempt.id), since=attempt.started_at - timedelta(minutes=1)
        )
        if run is None:
            _fail_without_run(db, attempt, contact, campaign)
            return "no_run_found"
        attempt.provider_call_id = str(run.run_id)
        db.add(
            CallEvent(
                call_attempt_id=attempt.id,
                event_type="RECONCILED_RUN_ADOPTED",
                payload={"workflow_run_id": run.run_id},
            )
        )
        db.flush()
    else:
        run = client.get_run(int(attempt.provider_call_id))

    if run.is_completed:
        result = apply_completion(
            db, attempt, contact, campaign, _completion_from_run(run), source=_SOURCE
        )
        return result.outcome

    age = (now - attempt.started_at).total_seconds()
    if age > settings.dograh_stale_attempt_seconds:
        metrics.incr(metrics.STALE_JOBS)
        result = apply_completion(
            db,
            attempt,
            contact,
            campaign,
            Completion(
                run_id=str(run.run_id),
                call_status="technical_error_stale_run",
                disposition=None,
                duration_seconds=0 if attempt.state == CallAttemptState.INITIATED else None,
                transcript_url=None,
            ),
            source=_SOURCE,
        )
        return result.outcome
    return "in_progress"


def reconcile_stale_attempts(
    db: Session, client: DograhClient, *, now: datetime | None = None, limit: int = 25
) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=get_settings().dograh_reconcile_after_seconds)
    stale = list(
        db.execute(
            select(CallAttempt)
            .where(
                CallAttempt.provider == "dograh",
                CallAttempt.state.in_((CallAttemptState.INITIATED, CallAttemptState.CONNECTED)),
                CallAttempt.started_at < cutoff,
            )
            .order_by(CallAttempt.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    handled = 0
    for attempt in stale:
        attempt_id = attempt.id
        try:
            outcome = _reconcile_one(db, client, attempt, now)
            db.commit()
        except DograhApiError as exc:
            # Dograh unreachable: leave the attempt exactly as it is and try
            # again next sweep. Never guess, never retry blindly.
            db.rollback()
            logger.warning(
                "reconcile_provider_error",
                extra={"attempt_id": str(attempt_id), "error_kind": exc.kind.value},
            )
            continue
        except Exception:
            db.rollback()
            logger.exception("reconcile_failed", extra={"attempt_id": str(attempt_id)})
            continue
        handled += 1
        logger.info("reconciled", extra={"attempt_id": str(attempt_id), "outcome": outcome})
        if outcome == "ended_normally":
            admit_analysis(db, attempt_id)
    return handled


def readmit_missing_analysis(db: Session, *, now: datetime | None = None, limit: int = 25) -> int:
    """Completed Dograh calls whose post-commit analysis admission was lost
    (e.g. Redis was down at that moment). Admission is idempotent."""
    now = now or datetime.now(UTC)
    rows = db.execute(
        select(CallAttempt.id)
        .join(Contact, Contact.id == CallAttempt.contact_id)
        .where(
            CallAttempt.provider == "dograh",
            CallAttempt.state.in_(
                (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
            ),
            CallAttempt.ended_at > now - timedelta(hours=24),
            Contact.status.in_((ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL)),
            ~exists().where(CallAnalysis.call_attempt_id == CallAttempt.id),
        )
        .limit(limit)
    ).all()
    for (attempt_id,) in rows:
        admit_analysis(db, uuid.UUID(str(attempt_id)))
    return len(rows)
