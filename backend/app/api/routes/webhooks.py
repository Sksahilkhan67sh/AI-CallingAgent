"""Telephony provider webhook -- Checkpoint 03 Step 41.

Order of operations (CP11, mirrors the Dograh webhook): rate limit -> shared-secret
authentication -> body validation -> idempotency / state transition (WebhookService).
Authentication is a route dependency so it runs BEFORE the body is validated: an
unauthenticated caller gets a bare 401, never schema feedback and never any processing.
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_dependency
from app.schemas.webhook import TelephonyCallStatusWebhook
from app.services.security_audit import record_security_event
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/api/v1/webhooks/telephony", tags=["Webhooks"])


def _webhook_rate_limit():
    return rate_limit_dependency(
        limit=lambda: get_settings().webhook_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="telephony_webhook",
    )


def _verify_secret(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    # Compared as bytes in constant time: the previous `!=` leaked timing, and
    # compare_digest on a non-ASCII str would raise TypeError (-> HTTP 500).
    expected = get_settings().telephony_webhook_secret.encode()
    if x_webhook_secret and secrets.compare_digest(x_webhook_secret.encode(), expected):
        return
    source_ip = request.client.host if request.client else "unknown"
    record_security_event(
        db,
        action="webhook.auth_failed",
        actor="anonymous",
        entity_type="webhook",
        metadata={
            "endpoint": "telephony",
            "credential_presented": bool(x_webhook_secret),
            "source_ip": source_ip,
        },
        throttle_key=f"telephony:{source_ip}",
        commit=True,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
    )


@router.post(
    "/call-status", dependencies=[Depends(_webhook_rate_limit()), Depends(_verify_secret)]
)
def receive_call_status(
    payload: TelephonyCallStatusWebhook,
    db: Session = Depends(get_db),
) -> dict:
    result = WebhookService(db).process_call_status(payload)
    if result.get("status") == "already_processed":
        record_security_event(
            db,
            action="webhook.duplicate",
            actor="telephony",
            entity_type="webhook",
            metadata={"endpoint": "telephony"},
        )
    return result
