"""CP11 -- security audit events (authn/authz denials, login, webhook auth failures).

Thin layer over the existing `record_audit_event`. It adds what security events need:

* correlation: the request_id is stored in the row's metadata and in a structured
  log line, so audit and logs line up;
* durability on error paths: these events are written right before an
  HTTPException/ConflictError is raised, and `get_db` rolls back whenever an
  exception propagates -- so `commit=True` commits the row first;
* flood control: unauthenticated callers can trigger events (bad login, bad webhook
  secret). With `throttle_key` at most one DB row per (action, key) per window is
  written per process, the dict is size-bounded, and EVERY occurrence is still
  logged -- the log is the complete record, the DB row is the durable sample;
* never raises into the request: a failed audit write must not turn a clean 401/403
  into a 500 (it is logged instead).

Callers pass only safe identifiers. Never pass tokens, secrets, passwords, phone
numbers, transcript text or request bodies.
"""

import logging
import time
import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.request_context import current_request_id
from app.services.audit_service import record_audit_event

logger = logging.getLogger("app.security")

_THROTTLE_SECONDS = 60.0
_THROTTLE_MAX_KEYS = 10_000
_last_written: dict[tuple[str, str], float] = {}


def _should_write(action: str, throttle_key: str | None) -> bool:
    if throttle_key is None:
        return True
    now = time.monotonic()
    key = (action, throttle_key)
    previous = _last_written.get(key)
    if previous is not None and now - previous < _THROTTLE_SECONDS:
        return False
    if len(_last_written) >= _THROTTLE_MAX_KEYS:  # bounded memory under a spray of keys
        _last_written.clear()
    _last_written[key] = now
    return True


def record_security_event(
    db: Session,
    *,
    action: str,
    actor: str,
    entity_type: str = "security",
    entity_id: uuid.UUID | None = None,
    metadata: dict[str, object] | None = None,
    throttle_key: str | None = None,
    commit: bool = False,
) -> None:
    details = {**(metadata or {}), "request_id": current_request_id()}
    # `sec_` prefix: a metadata key can never collide with a LogRecord attribute (logging
    # raises KeyError on that); the request_id is already stamped on every record by the
    # record factory in core/request_context.py.
    fields = {f"sec_{key}": value for key, value in details.items() if key != "request_id"}
    logger.warning("security_event", extra={"sec_action": action, "sec_actor": actor, **fields})
    if not _should_write(action, throttle_key):
        return
    try:
        record_audit_event(
            db,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=details,
        )
        if commit:
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("security_audit_write_failed", extra={"action": action})
