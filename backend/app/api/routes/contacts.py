"""Contact endpoints -- Checkpoint 01 spec, Step 10."""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.contact import ContactCreate, ContactResponse
from app.services.contact_service import ContactService

router = APIRouter(prefix="/api/v1/contacts", tags=["Contacts"])


@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
def create_contact(data: ContactCreate, db: Session = Depends(get_db)) -> ContactResponse:
    contact = ContactService(db).create_contact(data)
    return ContactResponse.model_validate(contact)


@router.get("/{contact_id}", response_model=ContactResponse)
def get_contact(contact_id: uuid.UUID, db: Session = Depends(get_db)) -> ContactResponse:
    contact = ContactService(db).get_contact(contact_id)
    return ContactResponse.model_validate(contact)
