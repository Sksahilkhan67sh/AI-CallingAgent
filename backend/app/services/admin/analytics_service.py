"""Operational analytics -- Checkpoint 07 §23-24, §45-46.

Metric definitions (mirrors the checkpoint's own §24 wording exactly,
using this repository's terminology):

- Connection rate: connected attempts / total dial attempts. "Connected"
  means the attempt reached `CallAttemptState.CONNECTED` at some point
  -- i.e. its final state is ENDED_NORMALLY or DROPPED_MID_CALL, never
  FAILED_TO_CONNECT or still INITIATED.
- Completion rate: attempts ending ENDED_NORMALLY / total dial attempts.
- Retry rate: attempts requiring recovery (DROPPED_MID_CALL) / eligible
  connected attempts (ENDED_NORMALLY + DROPPED_MID_CALL) -- same
  definition CP05's own retry-eligibility model uses.
- Opt-out rate: contacts closed via opt-out / total dial attempts. Not
  tracked as a distinct CallAttempt state (opt-out ends a call the same
  way any graceful end does, per CP06's own admission-eligibility
  reasoning -- ContactStatus.CLOSED is the closest signal available
  without adding a new column, so this is an approximation, documented
  as such in docs/CHECKPOINT-07-NOTES.md).
- Analysis completion rate: COMPLETED analyses / all analysis jobs
  created in range.
- Interest rate: INTERESTED analyses / all COMPLETED analyses in range.

A CONTACT is not an ATTEMPT (§45): every count below is explicitly
attempt-scoped or analysis-scoped, never a bare "contact count" used to
approximate either.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus, InterestStatus
from app.schemas.admin import AnalyticsResponse

_RANGE_TO_DAYS = {"today": 1, "7d": 7, "30d": 30}


def _range_start(range_key: str) -> datetime:
    days = _RANGE_TO_DAYS.get(range_key, 7)
    return datetime.now(UTC) - timedelta(days=days)


def get_analytics(
    db: Session, *, range_key: str = "7d", campaign_id: UUID | None = None
) -> AnalyticsResponse:
    since = _range_start(range_key)

    attempts_q = select(CallAttempt).join(Contact, CallAttempt.contact_id == Contact.id)
    attempts_q = attempts_q.where(CallAttempt.started_at >= since)
    if campaign_id is not None:
        attempts_q = attempts_q.where(Contact.campaign_id == campaign_id)
    attempts_sub = attempts_q.subquery()

    total_dial_attempts = db.execute(
        select(func.count()).select_from(attempts_sub)
    ).scalar_one()
    connected_attempts = db.execute(
        select(func.count())
        .select_from(attempts_sub)
        .where(
            attempts_sub.c.state.in_(
                (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
            )
        )
    ).scalar_one()
    completed_attempts = db.execute(
        select(func.count())
        .select_from(attempts_sub)
        .where(attempts_sub.c.state == CallAttemptState.ENDED_NORMALLY)
    ).scalar_one()
    dropped_mid_call = db.execute(
        select(func.count())
        .select_from(attempts_sub)
        .where(attempts_sub.c.state == CallAttemptState.DROPPED_MID_CALL)
    ).scalar_one()
    avg_duration = db.execute(
        select(
            func.avg(
                func.extract("epoch", attempts_sub.c.ended_at - attempts_sub.c.started_at)
            )
        ).where(attempts_sub.c.ended_at.is_not(None))
    ).scalar_one()

    contacts_q = select(Contact)
    if campaign_id is not None:
        contacts_q = contacts_q.where(Contact.campaign_id == campaign_id)
    contacts_q = contacts_q.where(Contact.updated_at >= since)
    contacts_sub = contacts_q.subquery()
    opt_out_count = db.execute(
        select(func.count())
        .select_from(contacts_sub)
        .where(contacts_sub.c.status == ContactStatus.CLOSED)
    ).scalar_one()

    analysis_q = select(CallAnalysis).where(CallAnalysis.created_at >= since)
    if campaign_id is not None:
        analysis_q = analysis_q.where(CallAnalysis.campaign_id == campaign_id)
    analysis_sub = analysis_q.subquery()

    analysis_jobs_total = db.execute(
        select(func.count()).select_from(analysis_sub)
    ).scalar_one()
    analyzed_calls = db.execute(
        select(func.count())
        .select_from(analysis_sub)
        .where(analysis_sub.c.status == AnalysisStatus.COMPLETED)
    ).scalar_one()
    interested_calls = db.execute(
        select(func.count())
        .select_from(analysis_sub)
        .where(
            analysis_sub.c.status == AnalysisStatus.COMPLETED,
            analysis_sub.c.interest_status == InterestStatus.INTERESTED,
        )
    ).scalar_one()
    avg_lead_score = db.execute(
        select(func.avg(analysis_sub.c.lead_score)).where(
            analysis_sub.c.status == AnalysisStatus.COMPLETED
        )
    ).scalar_one()

    return AnalyticsResponse(
        range=range_key,
        campaign_id=campaign_id,
        total_dial_attempts=total_dial_attempts,
        connected_attempts=connected_attempts,
        connection_rate=(connected_attempts / total_dial_attempts) if total_dial_attempts else None,
        completed_attempts=completed_attempts,
        completion_rate=(completed_attempts / total_dial_attempts) if total_dial_attempts else None,
        attempts_requiring_recovery=dropped_mid_call,
        eligible_connected_attempts=connected_attempts,
        retry_rate=(dropped_mid_call / connected_attempts) if connected_attempts else None,
        opt_out_count=opt_out_count,
        opt_out_rate=(opt_out_count / total_dial_attempts) if total_dial_attempts else None,
        average_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        analyzed_calls=analyzed_calls,
        analysis_jobs_total=analysis_jobs_total,
        analysis_completion_rate=(
            (analyzed_calls / analysis_jobs_total) if analysis_jobs_total else None
        ),
        interested_calls=interested_calls,
        interest_rate=(interested_calls / analyzed_calls) if analyzed_calls else None,
        average_lead_score=float(avg_lead_score) if avg_lead_score is not None else None,
    )
