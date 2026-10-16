"""HF1 1.5 -- `provider_halt`: stop dialing when the PROVIDER, not the contact, is the problem.

Why it exists. A rejected credential (401), an unknown trigger (404) or exhausted quota (402)
used to be handled like a per-contact failure: the contact became terminal and was never
retried, so a rotated key burned customers one by one until the circuit breaker opened.

What it does. The dialer calls `engage` on the FIRST provider-configuration error (and, as a
safety net, when `PROVIDER_HALT_SYSTEMIC_THRESHOLD` consecutive permanent per-contact failures
share one category across different contacts). Engaged, the shared outbound gate blocks dialing
exactly like the kill switch -- same PostgreSQL table, same fail-closed reads, same cache. Only
an admin releases it (mandatory reason, audited); releasing resurrects nothing because nothing
was burned: held contacts are still dialable and their jobs are still queued.

What it never stores. Reasons come from a FIXED vocabulary below, never from exception text or
provider bodies, so a credential cannot reach the database, an audit row, a log line or the API.
"""

import logging

import redis

from app.core.config import get_settings
from app.services import outbound_control
from app.services.outbound_control import ControlUnavailableError

logger = logging.getLogger(__name__)

KEY = outbound_control.PROVIDER_HALT

# Reason codes returned by `block_reason` (also logged / put in job outcomes).
HALTED = "provider_halt_engaged"
UNAVAILABLE = "provider_halt_unavailable"

# Categories (sanitized, stable, safe to show an operator).
AUTHENTICATION_ERROR = "authentication_error"
PROVIDER_REJECTED = "provider_rejected"
QUOTA_EXHAUSTED = "quota_exhausted"
CONFIGURATION_ERROR = "configuration_error"
SYSTEMIC_FAILURE = "systemic_failure"

_REASONS = {
    AUTHENTICATION_ERROR: "Provider rejected the API credential (HTTP {status}). "
    "Check or rotate the key, then release.",
    PROVIDER_REJECTED: "Provider does not recognise the configured workflow/trigger "
    "(HTTP {status}). Check the trigger id, then release.",
    QUOTA_EXHAUSTED: "Provider reports exhausted quota or credit (HTTP {status}). "
    "Top up, then release.",
    CONFIGURATION_ERROR: "The provider client is not configured. Fix the settings, then release.",
    SYSTEMIC_FAILURE: "{count} consecutive permanent provider failures ({source}) across "
    "different contacts. Investigate the provider, then release.",
}


def reason_for(
    category: str,
    *,
    status_code: int | None = None,
    count: int | None = None,
    source_category: str | None = None,
) -> str:
    """The sanitized operator-facing reason for a category. Only integers and category names
    from this module are interpolated -- never anything the provider sent."""
    template = _REASONS.get(category, "Provider problem ({category}).")
    return template.format(
        status=status_code if status_code is not None else "n/a",
        count=count if count is not None else "n/a",
        source=source_category or "unknown",
        category=category,
    )


def block_reason() -> str | None:
    """None -> not halted. Never raises; an unreadable store fails CLOSED."""
    try:
        row = outbound_control.read_all().get(KEY)
    except ControlUnavailableError:
        logger.error("provider_halt_unreadable_failing_closed")
        return UNAVAILABLE
    return HALTED if row is not None and row.engaged else None


def get_state() -> dict[str, object]:
    """Current halt state for the admin API. Raises ControlUnavailableError."""
    return outbound_control.state_as_dict(outbound_control.read_all(use_cache=False).get(KEY))


def engage(
    category: str,
    reason: str,
    *,
    actor: str = "system",
    metadata: dict[str, object] | None = None,
) -> bool:
    """True only if THIS call engaged the halt (False: it already was). The audit event is
    written in the same transaction as the transition, so a flood of provider errors writes
    exactly ONE `provider_halt.engaged` event."""
    changed = outbound_control.engage(
        KEY,
        actor=actor,
        reason=reason,
        category=category,
        audit=(
            "provider_halt.engaged",
            {"category": category, "reason": reason, **(metadata or {})},
        ),
    )
    if changed:
        logger.error("provider_halt_engaged", extra={"category": category, "actor": actor})
        reset_streak()
    return changed


def release(actor: str, reason: str | None = None) -> bool:
    """True only if THIS call released the halt. The audit event is written by the caller
    (the admin route), as for the kill switch."""
    changed = outbound_control.release(KEY, actor=actor, reason=reason)
    if changed:
        reset_streak()
    return changed


# --- systemic detector --------------------------------------------------------------------------

_STREAK_KEY = "provider_halt:streak:category"
_STREAK_CONTACTS_KEY = "provider_halt:streak:contacts"

# Atomic: a different category restarts the streak; the same category adds the contact to a set
# (so one contact failing twice is still one contact); both keys always carry a TTL.
_STREAK_SCRIPT = """
local category = redis.call('GET', KEYS[1])
if category ~= ARGV[1] then
  redis.call('DEL', KEYS[1], KEYS[2])
  redis.call('SET', KEYS[1], ARGV[1])
end
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return redis.call('SCARD', KEYS[2])
"""


def _redis() -> redis.Redis:
    from app.core.redis_client import get_redis

    return get_redis()


def note_permanent_failure(category: str, contact_id: str) -> int:
    """Record one permanent per-contact failure; returns how many DIFFERENT contacts in a row
    have now failed with this category (0 if Redis is unavailable -- the detector is a safety net
    and never blocks or breaks a dial)."""
    try:
        count = _redis().eval(
            _STREAK_SCRIPT,
            2,
            _STREAK_KEY,
            _STREAK_CONTACTS_KEY,
            category,
            contact_id,
            str(get_settings().provider_halt_streak_ttl_seconds),
        )  # type: ignore[misc]
        return int(count)  # type: ignore[arg-type]
    except redis.RedisError:
        logger.warning("provider_halt_streak_unavailable")
        return 0


def note_trigger_accepted() -> None:
    """The provider accepted a trigger: whatever failed before was not systemic."""
    reset_streak()


def reset_streak() -> None:
    try:
        _redis().delete(_STREAK_KEY, _STREAK_CONTACTS_KEY)
    except redis.RedisError:
        logger.warning("provider_halt_streak_unavailable")
