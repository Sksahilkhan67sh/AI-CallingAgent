"""CP11 -- global outbound kill switch.

Purpose: immediately stop NEW outbound calls. It does not delete queued jobs
(they stay pending in the stream and in PostgreSQL) and does not touch calls
that are already in progress -- there is no safe termination mechanism in the
project, so active calls follow the normal webhook-driven lifecycle.

State lives in a single Redis key (hot state, read on every dial -- no cache, so
propagation is one Redis round trip). Two rules make that safe:

* FAIL CLOSED. If Redis cannot be read (down, timeout, protocol error) the
  answer is "blocked", never "allowed". An unreadable switch is treated as ON.
* A static backstop, `OUTBOUND_KILL_SWITCH=true`, is checked first and needs no
  Redis at all. It covers the one thing a Redis flag cannot: Redis losing its
  data (flush / restart without persistence) and silently turning the switch
  OFF. Operators who need the switch to survive that should set the env var or
  run Redis with AOF persistence.

Enable/disable are single atomic Redis commands (SET NX / DEL), so concurrent
toggles produce exactly one state transition -- and therefore one audit event.
"""

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import cast

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

KEY = "outbound:kill_switch"

# Reason codes returned by `block_reason` (also logged / put in job outcomes).
ENABLED = "kill_switch_enabled"
UNAVAILABLE = "kill_switch_unavailable"

# A hung Redis must not hang a dial worker or an API request: short, bounded I/O.
_SOCKET_TIMEOUT_SECONDS = 1.0
_MAX_REASON_LENGTH = 200


class KillSwitchUnavailableError(Exception):
    """The switch state could not be read or changed (Redis problem)."""


@lru_cache
def _client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


def block_reason() -> str | None:
    """None -> outbound allowed. Otherwise a reason code. Never raises: any
    failure to read the switch is reported as `UNAVAILABLE` (fail closed)."""
    if get_settings().outbound_kill_switch:
        return ENABLED
    try:
        return ENABLED if _client().exists(KEY) else None
    except redis.RedisError:
        logger.error("kill_switch_unreadable_failing_closed")
        return UNAVAILABLE


def get_state() -> dict[str, object]:
    """Current state for the admin API. Raises KillSwitchUnavailableError."""
    static = get_settings().outbound_kill_switch
    try:
        raw = cast(str | None, _client().get(KEY))
    except redis.RedisError as exc:
        raise KillSwitchUnavailableError from exc
    detail: dict[str, object] = json.loads(raw) if raw else {}
    return {
        "enabled": static or raw is not None,
        "runtime_flag": raw is not None,
        "static_env_flag": static,
        **detail,
    }


def enable(actor: str, reason: str | None) -> bool:
    """Returns True only if THIS call turned the switch on (False: already on)."""
    payload = json.dumps(
        {
            "enabled_by": actor,
            "enabled_at": datetime.now(UTC).isoformat(),
            "reason": (reason or "")[:_MAX_REASON_LENGTH] or None,
        }
    )
    try:
        return bool(_client().set(KEY, payload, nx=True))
    except redis.RedisError as exc:
        raise KillSwitchUnavailableError from exc


def disable() -> bool:
    """Returns True only if THIS call turned the switch off (False: already off)."""
    try:
        return bool(_client().delete(KEY))
    except redis.RedisError as exc:
        raise KillSwitchUnavailableError from exc
