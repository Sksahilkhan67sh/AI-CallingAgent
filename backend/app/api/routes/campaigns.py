"""Campaign endpoints -- Checkpoint 01 Step 10, extended in Checkpoint 02."""

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import ValidationError
from app.models.enums import CampaignStatus, ContactStatus
from app.schemas.campaign import CampaignCreate, CampaignResponse, CampaignUpdate
from app.schemas.campaign_contact import (
    BulkContactImportResult,
    CampaignContactCounts,
    CampaignContactResponse,
)
from app.schemas.contact import ContactResponse
from app.schemas.pagination import Page
from app.services.campaign_contact_service import CampaignContactService
from app.services.campaign_service import CampaignService
from app.services.contact_import_service import ContactImportService
from app.services.contact_service import ContactService

router = APIRouter(prefix="/api/v1/campaigns", tags=["Campaigns"])

# A conservative upload-size guard -- well above what MAX_IMPORT_ROWS
# rows of a phone_number column would ever produce, just there so an
# oversized upload is rejected before being read into memory at all.
_MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(
    data: CampaignCreate, db: Session = Depends(get_db)
) -> CampaignResponse:
    campaign = CampaignService(db).create_campaign(data)
    return CampaignResponse.model_validate(campaign)


@router.get("", response_model=Page[CampaignResponse])
def list_campaigns(
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page[CampaignResponse]:
    items, total = CampaignService(db).list_campaigns(
        status=status_filter, limit=limit, offset=offset
    )
    return Page[CampaignResponse](
        items=[CampaignResponse.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_campaign(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> CampaignResponse:
    campaign = CampaignService(db).get_campaign(campaign_id)
    return CampaignResponse.model_validate(campaign)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
def update_campaign(
    campaign_id: uuid.UUID, data: CampaignUpdate, db: Session = Depends(get_db)
) -> CampaignResponse:
    campaign = CampaignService(db).update_campaign(campaign_id, data)
    return CampaignResponse.model_validate(campaign)


@router.post("/import", response_model=BulkContactImportResult, status_code=status.HTTP_201_CREATED)
async def import_contacts(
    name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> BulkContactImportResult:
    """FR-1.1-FR-1.4: creates the campaign only if the file has at least
    one valid contact -- see docs/CHECKPOINT-02-NOTES.md."""
    contents = await file.read(_MAX_IMPORT_FILE_BYTES + 1)
    if len(contents) > _MAX_IMPORT_FILE_BYTES:
        raise ValidationError(
            f"Import file exceeds the maximum size of {_MAX_IMPORT_FILE_BYTES} bytes"
        )
    return ContactImportService(db).import_csv(name, contents)


@router.get("/{campaign_id}/contacts", response_model=Page[ContactResponse])
def list_campaign_contacts(
    campaign_id: uuid.UUID,
    status_filter: ContactStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page[ContactResponse]:
    CampaignService(db).get_campaign(campaign_id)  # 404 if missing
    items, total = ContactService(db).list_contacts(
        campaign_id=campaign_id, status=status_filter, limit=limit, offset=offset
    )
    return Page[ContactResponse](
        items=[ContactResponse.model_validate(c) for c in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{campaign_id}/contacts/counts", response_model=CampaignContactCounts)
def get_campaign_contact_counts(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignContactCounts:
    counts = CampaignContactService(db).counts(campaign_id)
    return CampaignContactCounts(**counts)


@router.post(
    "/{campaign_id}/contacts/{contact_id}",
    response_model=CampaignContactResponse,
    status_code=status.HTTP_200_OK,
)
def associate_contact(
    campaign_id: uuid.UUID, contact_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignContactResponse:
    contact = CampaignContactService(db).associate(campaign_id, contact_id)
    return CampaignContactResponse(
        contact_id=contact.id, campaign_id=contact.campaign_id, status=contact.status.value
    )


@router.delete(
    "/{campaign_id}/contacts/{contact_id}",
    response_model=CampaignContactResponse,
)
def remove_contact(
    campaign_id: uuid.UUID, contact_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignContactResponse:
    contact = CampaignContactService(db).remove(campaign_id, contact_id)
    return CampaignContactResponse(
        contact_id=contact.id, campaign_id=contact.campaign_id, status=contact.status.value
    )
