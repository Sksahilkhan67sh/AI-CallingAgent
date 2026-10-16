"""Data lifecycle administration -- CP15A.

Erasure, export and legal hold are ADMIN-only and on the CP11 mutation budget; every one is
audited. The retention status endpoint is read-only and open to any authenticated role. No
response carries a phone number except the explicit single-contact export."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.api.route_limits import MUTATION_LIMIT
from app.core.config import get_settings
from app.core.database import DB_SESSION
from app.models.retention_run import RetentionRun
from app.schemas.admin_data import (
    ErasureRequest,
    ErasureResponse,
    LegalHoldRequest,
    LegalHoldResponse,
    RetentionStatusResponse,
)
from app.services.admin.auth import AdminPrincipal
from app.services.data_lifecycle import erasure, export, holds, retention

router = APIRouter(prefix="/api/v1/admin/data", tags=["Admin Data Lifecycle"])

_ADMIN = Depends(require_role("admin"))


@router.post("/erasure", response_model=ErasureResponse, dependencies=[MUTATION_LIMIT])
def erase_data(
    data: ErasureRequest,
    db: Session = DB_SESSION,
    principal: AdminPrincipal = _ADMIN,
) -> ErasureResponse:
    result = erasure.erase(
        db,
        actor=principal.username,
        now=datetime.now(UTC),
        phone_number=data.phone_number,
        contact_id=data.contact_id,
        reason=data.reason,
        suppress=data.suppress,
    )
    return ErasureResponse(
        already_erased=result.already_erased,
        suppressed=result.suppressed,
        last4=result.last4,
        counts=result.counts,
    )


@router.get("/contacts/{contact_id}/export", dependencies=[MUTATION_LIMIT])
def export_contact_data(
    contact_id: uuid.UUID,
    db: Session = DB_SESSION,
    principal: AdminPrincipal = _ADMIN,
) -> dict[str, Any]:
    return export.export_contact(db, contact_id=contact_id, actor=principal.username)


@router.put(
    "/legal-hold/campaigns/{campaign_id}",
    response_model=LegalHoldResponse,
    dependencies=[MUTATION_LIMIT],
)
def set_campaign_legal_hold(
    campaign_id: uuid.UUID,
    data: LegalHoldRequest,
    db: Session = DB_SESSION,
    principal: AdminPrincipal = _ADMIN,
) -> LegalHoldResponse:
    changed = holds.set_campaign_hold(
        db, campaign_id=campaign_id, active=data.active, reason=data.reason,
        actor=principal.username,
    )
    return LegalHoldResponse(active=data.active, changed=changed)


@router.put(
    "/legal-hold/contacts/{contact_id}",
    response_model=LegalHoldResponse,
    dependencies=[MUTATION_LIMIT],
)
def set_contact_legal_hold(
    contact_id: uuid.UUID,
    data: LegalHoldRequest,
    db: Session = DB_SESSION,
    principal: AdminPrincipal = _ADMIN,
) -> LegalHoldResponse:
    changed = holds.set_contact_hold(
        db, contact_id=contact_id, active=data.active, reason=data.reason,
        actor=principal.username,
    )
    return LegalHoldResponse(active=data.active, changed=changed)


@router.get(
    "/retention/status",
    response_model=RetentionStatusResponse,
    dependencies=[Depends(require_admin)],
)
def retention_status(db: Session = DB_SESSION) -> RetentionStatusResponse:
    last = db.execute(
        select(RetentionRun).order_by(RetentionRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    config = retention.RetentionConfig.from_settings(get_settings())
    return RetentionStatusResponse(
        last_run_id=last.id if last else None,
        last_run_mode=last.mode if last else None,
        last_run_status=last.status if last else None,
        last_run_started_at=last.started_at if last else None,
        last_run_finished_at=last.finished_at if last else None,
        last_run_counts=last.counts if last else None,
        backlog=retention.backlog_counts(db, config, datetime.now(UTC), None),
    )
