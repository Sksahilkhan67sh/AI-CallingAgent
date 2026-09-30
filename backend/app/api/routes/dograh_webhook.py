"""Dograh post-call webhook -- Checkpoints 08, 09.

Pipeline: rate limit -> authenticate -> validate payload -> (in one
transaction) lock attempt, verify correlation, dedupe, validate and apply
the state transition, record the processed-event marker -> COMMIT -> ACK ->
post-commit downstream job.

The 2xx ACK is only sent after the transaction has durably committed. Any
failure returns a non-2xx and persists nothing; Dograh does not retry by
default, so the reconciler (app/services/telephony/dograh_reconciler.py)
recovers a call whose completion was never accepted.

Dograh supplies no cryptographic signature or event timestamp we can
trust, so replay protection rests on: shared-secret authentication, the
unique processed-event marker per attempt, workflow_run_id correlation,
and terminal-state immutability. This limitation is documented in
docs/CHECKPOINT-09-NOTES.md.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.analysis.admission import enqueue_call_analysis
from app.services.call_state import InvalidTransition
from app.services.telephony.dograh_webhook_service import (
    DograhWebhookError,
    process_dograh_webhook,
)

logger = logging.getLogger("dograh_webhook")

router = APIRouter(prefix="/api/v1/webhooks/dograh", tags=["Dograh Webhook"])


def _matches(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), expected.encode())  # type: ignore[union-attr]


def verify_dograh_secret(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Dograh's Webhook node supports BEARER_TOKEN or API_KEY auth; either
    is accepted. Fails closed: an unset secret authenticates nobody."""
    expected = get_settings().dograh_webhook_secret
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    if expected and (_matches(bearer, expected) or _matches(x_api_key, expected)):
        return
    metrics.incr(metrics.WEBHOOK_FAILURES)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Dograh webhook credential"
    )


@router.post(
    "/call-completed",
    dependencies=[
        Depends(rate_limit("dograh_webhook", "webhook_rate_limit_per_minute")),
        Depends(verify_dograh_secret),
    ],
)
def dograh_call_completed(
    payload: DograhWebhookPayload, db: Session = Depends(get_db)
) -> dict:
    metrics.incr(metrics.WEBHOOK_COUNT)
    try:
        result = process_dograh_webhook(db, payload)
        db.commit()  # durable before we ACK
    except DograhWebhookError as exc:
        db.rollback()
        metrics.incr(metrics.WEBHOOK_FAILURES)
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except InvalidTransition as exc:
        db.rollback()
        metrics.incr(metrics.WEBHOOK_FAILURES)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        metrics.incr(metrics.WEBHOOK_FAILURES)
        logger.exception("dograh_webhook_processing_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Processing failed"
        ) from None

    if result.needs_analysis:
        _admit_analysis(db, result.call_attempt_id)
    return {"call_attempt_id": str(result.call_attempt_id), "outcome": result.outcome}


def _admit_analysis(db: Session, attempt_id) -> None:  # type: ignore[no-untyped-def]
    """Post-commit downstream job. The call is already durably recorded, so
    a failure here must not fail the ACK; the reconciler re-admits any
    completed call that has no analysis row."""
    try:
        attempt = db.get(CallAttempt, attempt_id)
        contact = db.get(Contact, attempt.contact_id) if attempt is not None else None
        if attempt is not None and contact is not None:
            enqueue_call_analysis(db, attempt, contact)
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("dograh_analysis_admission_failed", extra={"attempt_id": str(attempt_id)})
