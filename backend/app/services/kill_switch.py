"""CP11 / HF1 -- global outbound kill switch.

Purpose: immediately stop NEW outbound calls. It does not delete queued jobs
(they stay pending in the stream and in PostgreSQL) and does not touch calls
that are already in progress -- there is no safe termination mechanism in the
project, so active calls follow the normal webhook-driven lifecycle.

HF1 1.3: the switch is DURABLE. State lives in the `kill_switch` row of `outbound_control`
(PostgreSQL); Redis is not consulted. (Before HF1 it was a single Redis key, so a Redis
restart without persistence silently turned the switch OFF.) Dialing is blocked when ANY of:

* the row is engaged,
* the static backstop `OUTBOUND_KILL_SWITCH=true` is set (needs no database at all),
* the state cannot be read -- FAIL CLOSED: an unreadable switch is treated as ON, with an
  error log.

A short in-process cache (`KILL_SWITCH_CACHE_SECONDS`, default 2 s) is allowed; a change made
in this process is visible immediately, one made elsewhere within the window.

Engage/release are single atomic statements, so concurrent toggles produce exactly one state
transition -- and therefore one audit event.
"""

import logging

from app.core.config import get_settings
from app.services import outbound_control
from app.services.outbound_control import ControlUnavailableError

logger = logging.getLogger(__name__)

KEY = outbound_control.KILL_SWITCH

# Reason codes returned by `block_reason` (also logged / put in job outcomes).
ENABLED = "kill_switch_enabled"
UNAVAILABLE = "kill_switch_unavailable"

_MAX_REASON_LENGTH = outbound_control.MAX_REASON_LENGTH


class KillSwitchUnavailableError(Exception):
    """The switch state could not be read or changed (database problem)."""


def block_reason() -> str | None:
    """None -> outbound allowed. Otherwise a reason code. Never raises: any
    failure to read the switch is reported as `UNAVAILABLE` (fail closed)."""
    if get_settings().outbound_kill_switch:
        return ENABLED
    try:
        row = outbound_control.read_all().get(KEY)
    except ControlUnavailableError:
        logger.error("kill_switch_unreadable_failing_closed")
        return UNAVAILABLE
    return ENABLED if row is not None and row.engaged else None


def get_state() -> dict[str, object]:
    """Current state for the admin API. Raises KillSwitchUnavailableError."""
    static = get_settings().outbound_kill_switch
    try:
        row = outbound_control.read_all(use_cache=False).get(KEY)
    except ControlUnavailableError as exc:
        raise KillSwitchUnavailableError from exc
    engaged = row is not None and row.engaged
    detail: dict[str, object] = {}
    if row is not None and engaged:
        detail = {
            "enabled_by": row.engaged_by,
            "enabled_at": row.engaged_at.isoformat() if row.engaged_at else None,
            "reason": row.reason,
        }
    return {
        "enabled": static or engaged,
        "runtime_flag": engaged,
        "static_env_flag": static,
        **detail,
    }


def enable(actor: str, reason: str | None) -> bool:
    """Returns True only if THIS call turned the switch on (False: already on)."""
    try:
        return outbound_control.engage(KEY, actor=actor, reason=reason)
    except ControlUnavailableError as exc:
        raise KillSwitchUnavailableError from exc


def disable(actor: str = "unknown") -> bool:
    """Returns True only if THIS call turned the switch off (False: already off)."""
    try:
        return outbound_control.release(KEY, actor=actor)
    except ControlUnavailableError as exc:
        raise KillSwitchUnavailableError from exc
