"""Thin helper around writing AuditLog rows, so each service doesn't
duplicate the same four-field construction (Checkpoint 02 Step 27).
Not a generic audit framework -- just the one write path."""

import uuid

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record_audit_event(
    db: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            event_metadata=metadata,
        )
    )
    db.flush()
