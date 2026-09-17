"""Contact endpoints -- Checkpoint 01 Step 10, extended in Checkpoint 02."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.enums import ContactStatus
from app.schemas.contact import ContactCreate, ContactResponse, ContactUpdate
from app.schemas.pagination import Page
from app.services.contact_service import ContactService

router = APIRouter(prefix="/api/v1/contacts", tags=["Contacts"])


@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
def create_contact(data: ContactCreate, db: Session = Depends(get_db)) -> ContactResponse:
    contact = ContactService(db).create_contact(data)
    return ContactResponse.model_validate(contact)


@router.get("", response_model=Page[ContactResponse])
def list_contacts(
    campaign_id: uuid.UUID | None = None,
    status_filter: ContactStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page[ContactResponse]:
    items, total = ContactService(db).list_contacts(
        campaign_id=campaign_id, status=status_filter, limit=limit, offset=offset
    )
    return Page[ContactResponse](
        items=[ContactResponse.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{contact_id}", response_model=ContactResponse)
def get_contact(contact_id: uuid.UUID, db: Session = Depends(get_db)) -> ContactResponse:
    contact = ContactService(db).get_contact(contact_id)
    return ContactResponse.model_validate(contact)


@router.patch("/{contact_id}", response_model=ContactResponse)
def update_contact(
    contact_id: uuid.UUID, data: ContactUpdate, db: Session = Depends(get_db)
) -> ContactResponse:
    contact = ContactService(db).update_contact(contact_id, data)
    return ContactResponse.model_validate(contact)


@router.post("/{contact_id}/deactivate", response_model=ContactResponse)
def deactivate_contact(
    contact_id: uuid.UUID, db: Session = Depends(get_db)
) -> ContactResponse:
    contact = ContactService(db).deactivate_contact(contact_id)
    return ContactResponse.model_validate(contact)
