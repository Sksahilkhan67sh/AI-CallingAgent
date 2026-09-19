"""Telephony provider webhook -- Checkpoint 03 Step 41.

Signature verification happens here (needs the raw header); everything
else (idempotency, schema validation via the Pydantic body model, state
transition, persistence) is in WebhookService.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.schemas.webhook import TelephonyCallStatusWebhook
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/api/v1/webhooks/telephony", tags=["Webhooks"])


@router.post("/call-status")
def receive_call_status(
    payload: TelephonyCallStatusWebhook,
    x_webhook_secret: str = Header(...),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if x_webhook_secret != settings.telephony_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
        )

    return WebhookService(db).process_call_status(payload)
