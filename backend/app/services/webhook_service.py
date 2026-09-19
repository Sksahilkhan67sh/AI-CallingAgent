"""Telephony call-status webhook processing -- Checkpoint 03 Step 41.

Flow (Webhook-Specification.md): signature verification -> schema
validation -> idempotency check -> state transition -> DB transaction
-> ACK. Schema validation happens via the Pydantic request model at the
route layer; signature verification also happens at the route layer
(needs the raw header). Everything else is here.

Only call-initiation/status fields are handled -- no conversation
events, per this checkpoint's explicit scope.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.enums import CallAttemptState, ContactStatus, NeverConnectedFailureReason
from app.models.processed_event import ProcessedEvent
from app.repositories.call_attempt_repository import CallAttemptRepository
from app.repositories.contact_repository import ContactRepository
from app.schemas.webhook import TelephonyCallStatusWebhook
from app.services.audit_service import record_audit_event

_ACTOR = "telephony-webhook"


class WebhookService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.attempts = CallAttemptRepository(db)
        self.contacts = ContactRepository(db)

    def process_call_status(self, payload: TelephonyCallStatusWebhook) -> dict:
        existing_event = self.db.execute(
            select(ProcessedEvent).where(ProcessedEvent.event_id == payload.event_id)
        ).scalar_one_or_none()
        if existing_event is not None:
            return {"status": "already_processed"}  # idempotent no-op

        attempt = self.attempts.get_by_provider_call_id(payload.provider_call_id)
        if attempt is None:
            raise NotFoundError(f"No call attempt found for {payload.provider_call_id}")

        if attempt.ended_at is not None:
            # State-transition validation: this attempt already reached
            # a terminal outcome -- don't let a duplicate/late webhook
            # rewrite history.
            raise ConflictError("Call attempt already has a terminal outcome")

        contact = self.contacts.get_by_id(attempt.contact_id)

        if payload.status == "connected":
            attempt.state = CallAttemptState.CONNECTED
            if contact is not None:
                contact.status = ContactStatus.IN_CONVERSATION
        elif payload.status == "failed":
            attempt.state = CallAttemptState.FAILED_TO_CONNECT
            attempt.ended_at = datetime.now(UTC)
            if payload.failure_reason:
                try:
                    attempt.connection_failure_reason = NeverConnectedFailureReason(
                        payload.failure_reason
                    )
                except ValueError as exc:
                    raise ConflictError(
                        f"Unknown failure_reason '{payload.failure_reason}'"
                    ) from exc
        else:
            raise ConflictError(f"Unknown call status '{payload.status}'")

        self.db.add(ProcessedEvent(event_id=payload.event_id, event_type="telephony.call_status"))
        self.db.flush()

        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="call_attempt.status_updated_via_webhook",
            entity_type="call_attempt",
            entity_id=attempt.id,
            metadata={"status": payload.status},
        )

        return {"status": "processed"}
