"""Telephony provider webhook -- Checkpoint 03 Step 41.

Order of operations (HF1 1.1, mirrors the Dograh webhook): shared-secret authentication ->
limits -> body validation -> idempotency / state transition (WebhookService). A valid secret
is only subject to a generous ceiling; a failed authentication is counted per client address
and answered 401 (429 only once that address is over its failure limit). Authentication is a
route dependency so it runs BEFORE the body is validated: an unauthenticated caller gets a
bare 401, never schema feedback and never any processing.
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.client_address import client_address
from app.core.config import get_settings
from app.core.database import DB_SESSION
from app.core.rate_limit import enforce_valid_webhook_ceiling, throttle_failed_webhook_auth
from app.schemas.webhook import TelephonyCallStatusWebhook
from app.services.security_audit import record_security_event
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/api/v1/webhooks/telephony", tags=["Webhooks"])


_RATE_KEY = "telephony_webhook"


def _authenticate_then_limit(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    db: Session = DB_SESSION,
) -> None:
    # Compared as bytes in constant time: the previous `!=` leaked timing, and
    # compare_digest on a non-ASCII str would raise TypeError (-> HTTP 500).
    expected = get_settings().telephony_webhook_secret.encode()
    if x_webhook_secret and secrets.compare_digest(x_webhook_secret.encode(), expected):
        enforce_valid_webhook_ceiling(db, request, key_prefix=_RATE_KEY)
        return
    source_ip = client_address(request)
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
    throttle_failed_webhook_auth(request, key_prefix=_RATE_KEY)  # 429 only when over the limit
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
    )


@router.post("/call-status", dependencies=[Depends(_authenticate_then_limit)])
def receive_call_status(
    payload: TelephonyCallStatusWebhook,
    db: Session = DB_SESSION,
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
