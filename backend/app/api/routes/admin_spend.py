"""Today's dial/spend position against the CP14 caps. Read-only, any authenticated role.
Every money figure is an ESTIMATE, not billing."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.core.errors import ServiceUnavailableError
from app.services import spend_cap

router = APIRouter(prefix="/api/v1/admin/spend", tags=["Admin Spend"])


class SpendStatus(BaseModel):
    budget_day: str
    budget_timezone: str
    dials_today: int
    daily_dial_cap: int
    estimated_minutes: float
    estimated_spend: float | None
    daily_estimated_spend_cap: float | None
    estimated_cost_per_minute: float | None
    percent_used: float
    blocked: bool
    blocked_reason: str | None
    note: str = (
        "Figures are estimates, not billing. Calls placed by several workers within a few "
        "seconds can overshoot a cap slightly; the provider-side limit is the real backstop."
    )


@router.get("", response_model=SpendStatus, dependencies=[Depends(require_admin)])
def get_spend(db: Session = Depends(get_db)) -> SpendStatus:
    settings = get_settings()
    try:
        status = spend_cap.compute_status(db)  # fresh, never the worker's cached value
    except SQLAlchemyError as exc:
        raise ServiceUnavailableError("Spend figures are unavailable") from exc
    reason = status.reached
    return SpendStatus(
        budget_day=status.day.isoformat(),
        budget_timezone=settings.budget_timezone,
        dials_today=status.dials,
        daily_dial_cap=status.dial_cap,
        estimated_minutes=status.estimated_minutes,
        estimated_spend=status.estimated_spend,
        daily_estimated_spend_cap=status.spend_cap,
        estimated_cost_per_minute=settings.estimated_cost_per_minute,
        percent_used=status.percent_used,
        blocked=reason is not None,
        blocked_reason=reason,
    )
