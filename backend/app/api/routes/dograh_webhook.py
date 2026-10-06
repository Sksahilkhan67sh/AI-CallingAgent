"""Dograh post-call webhook -- Checkpoint 08.

Configured as a Webhook node inside the Dograh workflow itself (see
docs/CHECKPOINT-08-NOTES.md for the exact payload_template and auth
setup); Dograh POSTs here once, asynchronously, after a triggered call
ends.
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_dependency
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.security_audit import record_security_event
from app.services.telephony.dograh_webhook_service import (
    DograhWebhookError,
    process_dograh_webhook,
)

router = APIRouter(prefix="/api/v1/webhooks/dograh", tags=["Dograh Webhook"])


def _webhook_rate_limit():
    return rate_limit_dependency(
        limit=lambda: get_settings().webhook_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="dograh_webhook",
    )


def _verify_secret(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> None:
    """Dograh's Webhook node supports BEARER_TOKEN or API_KEY auth on
    the outgoing request (its own docs) -- either is accepted here so
    the person configuring the workflow can pick whichever fits their
    Dograh credential store."""
    # CP10: declared as a route dependency so it runs BEFORE the body is
    # validated (an unauthenticated caller must not get schema feedback or
    # reach any processing), and compared as bytes so a non-ASCII credential
    # is a clean 401 rather than a TypeError/500.
    expected = get_settings().dograh_webhook_secret.encode()
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()

    if bearer_token and secrets.compare_digest(bearer_token.encode(), expected):
        return
    if x_api_key and secrets.compare_digest(x_api_key.encode(), expected):
        return
    source_ip = request.client.host if request.client else "unknown"
    record_security_event(
        db,
        action="webhook.auth_failed",
        actor="anonymous",
        entity_type="webhook",
        metadata={
            "endpoint": "dograh",
            # whether a credential was presented -- never the credential itself
            "credential_presented": bool(bearer_token or x_api_key),
            "source_ip": source_ip,
        },
        throttle_key=f"dograh:{source_ip}",
        commit=True,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Dograh webhook credential"
    )


@router.post(
    "/call-completed", dependencies=[Depends(_webhook_rate_limit()), Depends(_verify_secret)]
)
def dograh_call_completed(
    payload: DograhWebhookPayload,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = process_dograh_webhook(db, payload)
    except DograhWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if result.outcome == "already_processed":
        # Authenticated duplicate/replay: a no-op for the call, but worth a trace.
        record_security_event(
            db,
            action="webhook.duplicate",
            actor="dograh",
            entity_type="call_attempt",
            entity_id=result.call_attempt_id,
            metadata={"endpoint": "dograh"},
        )
    return {"call_attempt_id": str(result.call_attempt_id), "outcome": result.outcome}
