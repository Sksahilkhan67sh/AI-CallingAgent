"""Contact endpoints -- Checkpoint 01 Step 10, extended in Checkpoint 02."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.api.route_limits import MUTATION_LIMIT
from app.core.database import get_db
from app.models.enums import ContactStatus
from app.schemas.contact import ContactCreate, ContactResponse, ContactUpdate
from app.schemas.pagination import Page
from app.services.admin.auth import AdminPrincipal
from app.services.contact_service import ContactService

router = APIRouter(prefix="/api/v1/contacts", tags=["Contacts"])

# CP11 authorization matrix: every route requires a valid token. Reads are open to
# any authenticated role; mutations (and enqueue/import) are admin-only.
_ADMIN = Depends(require_role("admin"))  # resolves to the verified principal
_ANY_ROLE = [Depends(require_admin)]


@router.post(
    "",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[MUTATION_LIMIT],
)
def create_contact(
    data: ContactCreate,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> ContactResponse:
    contact = ContactService(db, actor=principal.username).create_contact(data)
    return ContactResponse.model_validate(contact)


@router.get("", response_model=Page[ContactResponse], dependencies=_ANY_ROLE)
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


@router.get("/{contact_id}", response_model=ContactResponse, dependencies=_ANY_ROLE)
def get_contact(contact_id: uuid.UUID, db: Session = Depends(get_db)) -> ContactResponse:
    contact = ContactService(db).get_contact(contact_id)
    return ContactResponse.model_validate(contact)


@router.patch(
    "/{contact_id}",
    response_model=ContactResponse,
    dependencies=[MUTATION_LIMIT],
)
def update_contact(
    contact_id: uuid.UUID,
    data: ContactUpdate,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> ContactResponse:
    contact = ContactService(db, actor=principal.username).update_contact(contact_id, data)
    return ContactResponse.model_validate(contact)


@router.post(
    "/{contact_id}/deactivate",
    response_model=ContactResponse,
    dependencies=[MUTATION_LIMIT],
)
def deactivate_contact(
    contact_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> ContactResponse:
    contact = ContactService(db, actor=principal.username).deactivate_contact(contact_id)
    return ContactResponse.model_validate(contact)
