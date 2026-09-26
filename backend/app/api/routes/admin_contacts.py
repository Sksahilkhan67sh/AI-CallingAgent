"""Contacts -- Checkpoint 07 §13-14. Read-only; phone numbers are
always masked in this surface (§13)."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin
from app.core.database import get_db
from app.models.enums import ContactStatus
from app.schemas.admin import ContactDetail, ContactListItem
from app.schemas.pagination import Page
from app.services.admin.auth import AdminPrincipal
from app.services.admin.contact_service import get_contact_detail, list_contacts

router = APIRouter(prefix="/api/v1/admin/contacts", tags=["Admin Contacts"])


@router.get("", response_model=Page[ContactListItem])
def list_admin_contacts(
    campaign_id: uuid.UUID | None = Query(default=None),
    status_filter: ContactStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> Page[ContactListItem]:
    items, total = list_contacts(
        db, campaign_id=campaign_id, status=status_filter, limit=limit, offset=offset
    )
    return Page[ContactListItem](items=items, total=total, limit=limit, offset=offset)


@router.get("/{contact_id}", response_model=ContactDetail)
def get_admin_contact(
    contact_id: uuid.UUID,
    db: Session = Depends(get_db),
    _principal: AdminPrincipal = Depends(require_admin),
) -> ContactDetail:
    return get_contact_detail(db, contact_id)
