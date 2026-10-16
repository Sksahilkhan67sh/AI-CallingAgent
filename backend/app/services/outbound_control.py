"""HF1 1.3 -- durable outbound controls (`kill_switch`, `provider_halt`) in PostgreSQL.

PostgreSQL is the source of truth; Redis is not consulted for this decision at all, so a Redis
restart or flush can never silently turn a stop OFF.

Reads
-----
`read_all()` returns the current rows. A short in-process cache (`KILL_SWITCH_CACHE_SECONDS`,
default 2 s) keeps the dialer loop from hitting the database on every poll; a change made in
THIS process invalidates it at once, a change made by another process is seen within the
window. A failed read is never cached and raises `ControlUnavailableError` -- callers fail
CLOSED (dialing blocked).

Writes
------
`engage` / `release` are each one SQL statement, so concurrent callers produce exactly one
state transition (and therefore one audit event):

* engage  = INSERT ... ON CONFLICT DO UPDATE ... WHERE NOT engaged   (RETURNING key)
* release = UPDATE ... SET engaged = false ... WHERE engaged         (RETURNING key)

Each uses its own short session and commits on its own; the service never touches the
caller's transaction (the dialer's session may hold an open transaction and the admin API
audits through the request session).
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.outbound_control import OutboundControl

logger = logging.getLogger(__name__)

KILL_SWITCH = "kill_switch"
PROVIDER_HALT = "provider_halt"

MAX_REASON_LENGTH = 200
MAX_CATEGORY_LENGTH = 64

# Injectable for tests ("time must be injectable wherever a decision depends on the clock").
_monotonic = time.monotonic
_utcnow = lambda: datetime.now(UTC)  # noqa: E731


class ControlUnavailableError(Exception):
    """The control state could not be read or changed (database problem)."""


@dataclass(frozen=True)
class ControlState:
    key: str
    engaged: bool
    reason: str | None
    category: str | None
    engaged_by: str | None
    engaged_at: datetime | None
    released_by: str | None
    released_at: datetime | None


def _open_session() -> Session:
    return SessionLocal()


_lock = threading.Lock()
_cache: tuple[float, dict[str, ControlState]] | None = None


def reset_state() -> None:
    """Forget the in-process cache (tests; and after any local change)."""
    global _cache
    with _lock:
        _cache = None


def _to_state(row: OutboundControl) -> ControlState:
    return ControlState(
        key=row.key,
        engaged=bool(row.engaged),
        reason=row.reason,
        category=row.category,
        engaged_by=row.engaged_by,
        engaged_at=row.engaged_at,
        released_by=row.released_by,
        released_at=row.released_at,
    )


def read_all(*, use_cache: bool = True) -> dict[str, ControlState]:
    """Every control row by key. Raises ControlUnavailableError (never returns stale
    "unknown" as "off")."""
    global _cache
    ttl = max(float(get_settings().kill_switch_cache_seconds), 0.0)
    now = _monotonic()
    if use_cache and ttl > 0:
        with _lock:
            cached = _cache
        if cached is not None and 0 <= now - cached[0] < ttl:
            return cached[1]
    try:
        with _open_session() as session:
            rows = session.execute(select(OutboundControl)).scalars().all()
            states = {row.key: _to_state(row) for row in rows}
    except Exception as exc:  # noqa: BLE001 -- any failure to read means "cannot tell"
        raise ControlUnavailableError(type(exc).__name__) from exc
    with _lock:
        _cache = (now, states)
    return states


def _clip(value: str | None, limit: int) -> str | None:
    return (value or "")[:limit] or None


def engage(
    key: str,
    *,
    actor: str,
    reason: str | None,
    category: str | None = None,
    audit: tuple[str, dict[str, object]] | None = None,
) -> bool:
    """True only if THIS call moved the control from off (or absent) to on. `audit`, when
    given as (action, metadata), is written in the SAME transaction as the transition, so a
    transition and its audit event exist together or not at all."""
    values = {
        "engaged": True,
        "reason": _clip(reason, MAX_REASON_LENGTH),
        "category": _clip(category, MAX_CATEGORY_LENGTH),
        "engaged_by": actor,
        "engaged_at": _utcnow(),
        "released_by": None,
        "released_at": None,
    }
    insert = pg_insert(OutboundControl).values(key=key, **values)
    stmt = insert.on_conflict_do_update(
        index_elements=[OutboundControl.key],
        set_=values,
        where=OutboundControl.engaged.is_(False),
    ).returning(OutboundControl.key)
    try:
        with _open_session() as session:
            changed = session.execute(stmt).first() is not None
            if changed and audit is not None:
                from app.services.audit_service import record_audit_event

                action, metadata = audit
                record_audit_event(
                    session,
                    actor=actor,
                    action=action,
                    entity_type="system",
                    entity_id=None,
                    metadata=metadata,
                )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        raise ControlUnavailableError(type(exc).__name__) from exc
    reset_state()
    return changed


def release(key: str, *, actor: str, reason: str | None = None) -> bool:
    """True only if THIS call moved the control from on to off."""
    stmt = (
        update(OutboundControl)
        .where(OutboundControl.key == key, OutboundControl.engaged.is_(True))
        .values(
            engaged=False,
            released_by=actor,
            released_at=_utcnow(),
            # the reason it WAS engaged is kept for the record; a release note, when given,
            # is carried by the audit event
        )
        .returning(OutboundControl.key)
    )
    try:
        with _open_session() as session:
            changed = session.execute(stmt).first() is not None
            session.commit()
    except Exception as exc:  # noqa: BLE001
        raise ControlUnavailableError(type(exc).__name__) from exc
    reset_state()
    return changed


def state_as_dict(state: ControlState | None) -> dict[str, object]:
    """API shape for one control (never contains anything but sanitized, stored fields)."""
    if state is None:
        return {
            "engaged": False,
            "reason": None,
            "category": None,
            "engaged_by": None,
            "engaged_at": None,
            "released_by": None,
            "released_at": None,
        }
    return {
        "engaged": state.engaged,
        "reason": state.reason,
        "category": state.category,
        "engaged_by": state.engaged_by,
        "engaged_at": state.engaged_at.isoformat() if state.engaged_at else None,
        "released_by": state.released_by,
        "released_at": state.released_at.isoformat() if state.released_at else None,
    }
