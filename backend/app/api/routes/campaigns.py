"""Campaign endpoints -- Checkpoint 01 spec, Step 10."""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.campaign import CampaignCreate, CampaignResponse
from app.services.campaign_service import CampaignService

router = APIRouter(prefix="/api/v1/campaigns", tags=["Campaigns"])


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(
    data: CampaignCreate, db: Session = Depends(get_db)
) -> CampaignResponse:
    campaign = CampaignService(db).create_campaign(data)
    return CampaignResponse.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_campaign(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignResponse:
    campaign = CampaignService(db).get_campaign(campaign_id)
    return CampaignResponse.model_validate(campaign)
