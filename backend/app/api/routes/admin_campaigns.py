"""Campaigns -- Checkpoint 07 §11-12. List/detail add dashboard metrics
on top of the existing CampaignService; the status-transition control
is a thin authenticated pass-through to the existing, already-validated
`CampaignService.update_campaign` -- the backend (not this route) owns
the allowed-transition state machine, exactly as §12 requires ("Backend
validates the transition").
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.core.database import get_db
from app.models.enums import CampaignStatus
from app.schemas.admin import CampaignDetail, CampaignListItem
from app.schemas.campaign import CampaignResponse, CampaignUpdate
from app.schemas.pagination import Page
from app.services.admin.auth import AdminPrincipal
from app.services.admin.campaign_service import get_campaign_detail, list_campaigns
from app.services.campaign_service import CampaignService

router = APIRouter(prefix="/api/v1/admin/campaigns", tags=["Admin Campaigns"])


@router.get("", response_model=Page[CampaignListItem])
def list_admin_campaigns(
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> Page[CampaignListItem]:
    items, total = list_campaigns(db, status=status_filter, limit=limit, offset=offset)
    return Page[CampaignListItem](items=items, total=total, limit=limit, offset=offset)


@router.get("/{campaign_id}", response_model=CampaignDetail)
def get_admin_campaign(
    campaign_id: uuid.UUID,
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> CampaignDetail:
    return get_campaign_detail(db, campaign_id)


@router.post("/{campaign_id}/status", response_model=CampaignResponse)
def transition_campaign_status(
    campaign_id: uuid.UUID,
    new_status: CampaignStatus,
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_role("admin")),
) -> CampaignResponse:
    """§5: operational controls (activate/pause/resume/complete) require
    the `admin` role, unlike the read-only monitoring endpoints above."""
    campaign = CampaignService(db).update_campaign(
        campaign_id, CampaignUpdate(status=new_status)
    )
    return CampaignResponse.model_validate(campaign)
