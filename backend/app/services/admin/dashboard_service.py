"""Dashboard overview aggregates -- Checkpoint 07 §9-10.

Single-query-per-section server-side aggregation (`GROUP BY`/`func.count`/
`func.avg`), never N+1 downloads of individual rows into the browser.
Reuses the existing ORM models directly rather than duplicating any
business logic from ContactService/CampaignService/CallAnalysisRepository
-- this module only reads and aggregates, it never mutates state.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import (
    AnalysisStatus,
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    InterestStatus,
)
from app.schemas.admin import (
    CallOverview,
    CampaignOverview,
    DashboardOverview,
    IntelligenceOverview,
    ReliabilityOverview,
)
from app.services.analysis.factory import get_analysis_queue
from app.services.queue.factory import get_queue

_ACTIVE_CONTACT_STATUSES = (
    ContactStatus.PENDING,
    ContactStatus.DIALING,
    ContactStatus.IN_CONVERSATION,
    ContactStatus.RECONNECTING,
)


def _contact_status_counts(db: Session) -> dict[ContactStatus, int]:
    rows = db.execute(
        select(Contact.status, func.count()).group_by(Contact.status)
    ).all()
    return {status: count for status, count in rows}


def _call_overview(db: Session) -> CallOverview:
    counts = _contact_status_counts(db)
    total = sum(counts.values())
    active = sum(counts.get(s, 0) for s in _ACTIVE_CONTACT_STATUSES)
    return CallOverview(
        total=total,
        active=active,
        completed=counts.get(ContactStatus.COMPLETED, 0),
        completed_partial=counts.get(ContactStatus.COMPLETED_PARTIAL, 0),
        retry_scheduled=counts.get(ContactStatus.RETRY_SCHEDULED, 0),
        closed=counts.get(ContactStatus.CLOSED, 0),
        queued=counts.get(ContactStatus.PENDING, 0),
    )


def _campaign_overview(db: Session) -> CampaignOverview:
    rows = db.execute(
        select(Campaign.status, func.count()).group_by(Campaign.status)
    ).all()
    counts = {status: count for status, count in rows}
    return CampaignOverview(
        active=counts.get(CampaignStatus.ACTIVE, 0),
        paused=counts.get(CampaignStatus.PAUSED, 0),
        completed=counts.get(CampaignStatus.COMPLETED, 0),
        draft=counts.get(CampaignStatus.DRAFT, 0),
    )


def _intelligence_overview(db: Session) -> IntelligenceOverview:
    completed = (
        select(CallAnalysis)
        .where(CallAnalysis.status == AnalysisStatus.COMPLETED)
        .subquery()
    )
    rows = db.execute(
        select(completed.c.interest_status, func.count()).group_by(
            completed.c.interest_status
        )
    ).all()
    counts = {status: count for status, count in rows}
    analyzed = sum(counts.values())
    avg_score = db.execute(select(func.avg(completed.c.lead_score))).scalar_one()

    return IntelligenceOverview(
        analyzed=analyzed,
        interested=counts.get(InterestStatus.INTERESTED, 0),
        maybe=counts.get(InterestStatus.MAYBE, 0),
        not_interested=counts.get(InterestStatus.NOT_INTERESTED, 0),
        unknown=counts.get(InterestStatus.UNKNOWN, 0) + counts.get(None, 0),
        average_lead_score=float(avg_score) if avg_score is not None else None,
    )


def _reliability_overview(db: Session) -> ReliabilityOverview:
    total_attempts = db.execute(select(func.count()).select_from(CallAttempt)).scalar_one()
    failed_to_connect = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .where(CallAttempt.state == CallAttemptState.FAILED_TO_CONNECT)
    ).scalar_one()
    dropped_mid_call = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .where(CallAttempt.state == CallAttemptState.DROPPED_MID_CALL)
    ).scalar_one()
    eligible_connected = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .where(
            CallAttempt.state.in_(
                (CallAttemptState.ENDED_NORMALLY, CallAttemptState.DROPPED_MID_CALL)
            )
        )
    ).scalar_one()

    analysis_total = db.execute(select(func.count()).select_from(CallAnalysis)).scalar_one()
    analysis_failed = db.execute(
        select(func.count())
        .select_from(CallAnalysis)
        .where(CallAnalysis.status == AnalysisStatus.FAILED)
    ).scalar_one()

    outbound_queue = get_queue()
    analysis_queue = get_analysis_queue()
    outbound_depth = _pending_count(
        outbound_queue.redis, outbound_queue.stream_key, outbound_queue.group
    )
    analysis_depth = _pending_count(
        analysis_queue.redis, analysis_queue.stream_key, analysis_queue.group
    )

    return ReliabilityOverview(
        retry_rate=(dropped_mid_call / eligible_connected) if eligible_connected else None,
        failure_rate=(failed_to_connect / total_attempts) if total_attempts else None,
        analysis_failure_rate=(analysis_failed / analysis_total) if analysis_total else None,
        analysis_queue_depth=analysis_depth,
        outbound_queue_depth=outbound_depth,
    )


def _pending_count(redis_client, stream_key: str, group: str) -> int:
    try:
        summary = redis_client.xpending(stream_key, group)
        return int(summary["pending"]) if summary else 0
    except Exception:
        return 0


def get_dashboard_overview(db: Session) -> DashboardOverview:
    return DashboardOverview(
        calls=_call_overview(db),
        campaigns=_campaign_overview(db),
        intelligence=_intelligence_overview(db),
        reliability=_reliability_overview(db),
    )
