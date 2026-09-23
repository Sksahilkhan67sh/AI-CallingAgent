"""Read-only post-call analysis endpoints -- Checkpoint 06 §37.

Only what CP06 needs to hand results to a consumer (CP07's dashboard,
or a script): analysis by call attempt, latest analysis by contact,
and a paginated list by campaign. No dashboard UX, no filtering UI --
that's explicitly CP07's scope (§38).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import NotFoundError
from app.models.call_analysis import CallAnalysis
from app.schemas.call_analysis import CallAnalysisResponse
from app.schemas.pagination import Page

router = APIRouter(prefix="/api/v1", tags=["Call Analysis"])


@router.get(
    "/call-attempts/{call_attempt_id}/analysis",
    response_model=CallAnalysisResponse,
)
def get_analysis_by_call_attempt(
    call_attempt_id: uuid.UUID, db: Session = Depends(get_db)
) -> CallAnalysisResponse:
    analysis = (
        db.execute(select(CallAnalysis).where(CallAnalysis.call_attempt_id == call_attempt_id))
        .scalars()
        .one_or_none()
    )
    if analysis is None:
        raise NotFoundError(f"No analysis for call attempt {call_attempt_id}")
    return CallAnalysisResponse.model_validate(analysis)


@router.get(
    "/contacts/{contact_id}/analysis",
    response_model=CallAnalysisResponse,
)
def get_latest_analysis_by_contact(
    contact_id: uuid.UUID, db: Session = Depends(get_db)
) -> CallAnalysisResponse:
    analysis = (
        db.execute(
            select(CallAnalysis)
            .where(CallAnalysis.contact_id == contact_id)
            .order_by(CallAnalysis.created_at.desc())
            .limit(1)
        )
        .scalars()
        .one_or_none()
    )
    if analysis is None:
        raise NotFoundError(f"No analysis for contact {contact_id}")
    return CallAnalysisResponse.model_validate(analysis)


@router.get(
    "/campaigns/{campaign_id}/analysis",
    response_model=Page[CallAnalysisResponse],
)
def list_analysis_by_campaign(
    campaign_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page[CallAnalysisResponse]:
    base = select(CallAnalysis).where(CallAnalysis.campaign_id == campaign_id)
    total = db.execute(
        select(func.count())
        .select_from(CallAnalysis)
        .where(CallAnalysis.campaign_id == campaign_id)
    ).scalar_one()
    items = (
        db.execute(base.order_by(CallAnalysis.created_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return Page[CallAnalysisResponse](
        items=[CallAnalysisResponse.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )
