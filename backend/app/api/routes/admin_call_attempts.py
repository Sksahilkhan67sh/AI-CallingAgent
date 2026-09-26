"""Call attempts -- Checkpoint 07 §15-17, §21. New capability (no
CallAttempt API existed before this checkpoint); authenticated,
server-side paginated/filtered.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin
from app.core.database import get_db
from app.models.enums import AnalysisStatus, CallAttemptState
from app.schemas.admin import CallAttemptDetail, CallAttemptListItem
from app.schemas.pagination import Page
from app.services.admin.auth import AdminPrincipal
from app.services.admin.call_attempt_service import get_call_attempt_detail, list_call_attempts

router = APIRouter(prefix="/api/v1/admin/call-attempts", tags=["Admin Call Attempts"])


@router.get("", response_model=Page[CallAttemptListItem])
def list_attempts(
    campaign_id: uuid.UUID | None = Query(default=None),
    contact_id: uuid.UUID | None = Query(default=None),
    state: CallAttemptState | None = Query(default=None),
    analysis_status: AnalysisStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> Page[CallAttemptListItem]:
    items, total = list_call_attempts(
        db,
        campaign_id=campaign_id,
        contact_id=contact_id,
        state=state,
        analysis_status=analysis_status,
        limit=limit,
        offset=offset,
    )
    return Page[CallAttemptListItem](items=items, total=total, limit=limit, offset=offset)


@router.get("/{call_attempt_id}", response_model=CallAttemptDetail)
def get_attempt(
    call_attempt_id: uuid.UUID,
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> CallAttemptDetail:
    return get_call_attempt_detail(db, call_attempt_id)
