"""Dashboard overview, analytics, and system health -- Checkpoint 07
§9-10, §23-26, §40. All read-only, all authenticated (any role -- these
are monitoring surfaces, not operational controls, per §5).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin
from app.core.database import get_db
from app.schemas.admin import AnalyticsResponse, DashboardOverview, SystemHealthResponse
from app.services.admin.analytics_service import get_analytics
from app.services.admin.auth import AdminPrincipal
from app.services.admin.dashboard_service import get_dashboard_overview
from app.services.admin.system_service import get_system_health

router = APIRouter(prefix="/api/v1/admin/dashboard", tags=["Admin Dashboard"])


@router.get("/overview", response_model=DashboardOverview)
def overview(
    db: Session = Depends(get_db), _principal: AdminPrincipal = Depends(require_admin)
) -> DashboardOverview:
    return get_dashboard_overview(db)


@router.get("/analytics", response_model=AnalyticsResponse)
def analytics(
    time_range: str = Query(default="7d", pattern="^(today|7d|30d)$", alias="range"),
    campaign_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> AnalyticsResponse:
    return get_analytics(db, range_key=time_range, campaign_id=campaign_id)


@router.get("/system", response_model=SystemHealthResponse)
def system_health(
    db: Session = Depends(get_db), _principal: AdminPrincipal = Depends(require_admin)
) -> SystemHealthResponse:
    return get_system_health(db)
