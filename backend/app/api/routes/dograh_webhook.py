"""Dograh post-call webhook -- Checkpoint 08.

Configured as a Webhook node inside the Dograh workflow itself (see
docs/CHECKPOINT-08-NOTES.md for the exact payload_template and auth
setup); Dograh POSTs here once, asynchronously, after a triggered call
ends.
"""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.client_address import client_address
from app.core.config import get_settings
from app.core.database import DB_SESSION
from app.core.rate_limit import enforce_valid_webhook_ceiling, throttle_failed_webhook_auth
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.security_audit import record_security_event
from app.services.telephony.dograh_webhook_service import (
    DograhWebhookError,
    process_dograh_webhook,
)

router = APIRouter(prefix="/api/v1/webhooks/dograh", tags=["Dograh Webhook"])


_RATE_KEY = "dograh_webhook"


def _authenticate_then_limit(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = DB_SESSION,
) -> None:
    """Dograh's Webhook node supports BEARER_TOKEN or API_KEY auth on
    the outgoing request (its own docs) -- either is accepted here so
    the person configuring the workflow can pick whichever fits their
    Dograh credential store.

    HF1 1.1 -- authenticate FIRST, then limit: a valid secret is only ever subject to the
    generous valid-request ceiling; a failed authentication is counted per client address and
    answered 401 (429 only once that address is over its failure limit)."""
    # CP10: declared as a route dependency so it runs BEFORE the body is
    # validated (an unauthenticated caller must not get schema feedback or
    # reach any processing), and compared as bytes so a non-ASCII credential
    # is a clean 401 rather than a TypeError/500.
    expected = get_settings().dograh_webhook_secret.encode()
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()

    if (bearer_token and secrets.compare_digest(bearer_token.encode(), expected)) or (
        x_api_key and secrets.compare_digest(x_api_key.encode(), expected)
    ):
        enforce_valid_webhook_ceiling(db, request, key_prefix=_RATE_KEY)
        return
    source_ip = client_address(request)
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
    throttle_failed_webhook_auth(request, key_prefix=_RATE_KEY)  # 429 only when over the limit
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Dograh webhook credential"
    )


@router.post("/call-completed", dependencies=[Depends(_authenticate_then_limit)])
def dograh_call_completed(
    payload: DograhWebhookPayload,
    db: Session = DB_SESSION,
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
