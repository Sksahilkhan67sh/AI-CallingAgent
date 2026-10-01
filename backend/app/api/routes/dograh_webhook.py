"""Dograh post-call webhook -- Checkpoint 08.

Configured as a Webhook node inside the Dograh workflow itself (see
docs/CHECKPOINT-08-NOTES.md for the exact payload_template and auth
setup); Dograh POSTs here once, asynchronously, after a triggered call
ends.
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_dependency
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.telephony.dograh_webhook_service import (
    DograhWebhookError,
    process_dograh_webhook,
)

router = APIRouter(prefix="/api/v1/webhooks/dograh", tags=["Dograh Webhook"])


def _webhook_rate_limit():
    return rate_limit_dependency(
        limit=get_settings().webhook_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="dograh_webhook",
    )


def _verify_secret(authorization: str | None, x_api_key: str | None) -> None:
    """Dograh's Webhook node supports BEARER_TOKEN or API_KEY auth on
    the outgoing request (its own docs) -- either is accepted here so
    the person configuring the workflow can pick whichever fits their
    Dograh credential store."""
    expected = get_settings().dograh_webhook_secret
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()

    if bearer_token and secrets.compare_digest(bearer_token, expected):
        return
    if x_api_key and secrets.compare_digest(x_api_key, expected):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Dograh webhook credential"
    )


@router.post("/call-completed", dependencies=[Depends(_webhook_rate_limit())])
def dograh_call_completed(
    payload: DograhWebhookPayload,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _verify_secret(authorization, x_api_key)

    try:
        result = process_dograh_webhook(db, payload)
    except DograhWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {"call_attempt_id": str(result.call_attempt_id), "outcome": result.outcome}
