"""Lightweight fixed-window rate limiter -- Checkpoint 09 §8.5, hardened in CP11.

Redis-backed (rate limiting is an explicitly appropriate use of Redis, never
permanent business state). Deliberately simple -- one atomic Lua script (INCR plus a TTL
that is set on creation and healed if ever missing) per window, no new dependency, no
token/leaky-bucket abstraction. HF1 1.1: the counter is a single script, so a key can never
exist without a TTL, whatever happens to the calling process.

HF1 1.1 -- AUTHENTICATE FIRST, THEN LIMIT. The pre-authentication endpoints (the two webhooks
and login) no longer count every request. They verify the credential first and count only
what is hostile: FAILED authentications, in per-client-address counters. A caller that
presents a valid secret is never throttled by anyone's failures (webhooks have only a
generous ceiling as an abuse backstop); a correct login is blocked only by failures from
its own client address.

Redis-failure policy is chosen PER ENDPOINT (`fail_closed`):

* fail closed (503): endpoints where an unthrottled flood is the danger -- admin
  login (credential guessing) and the authenticated expensive operations.
* fail open (+ audit): the provider webhooks. Dropping a Dograh completion
  webhook because the limiter's Redis is down would lose a call result for good
  (Dograh retry behaviour is not something this project can rely on). Webhook
  *authentication* is a separate dependency and never fails open.

429 responses carry `Retry-After` (seconds left in the window).
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import redis
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.client_address import rate_limit_identity
from app.core.config import get_settings
from app.core.database import get_db
from app.services.audit_service import record_audit_event

logger = logging.getLogger(__name__)

# One audit row per endpoint per process per minute while the limiter is down --
# every request is still logged, but a Redis outage must not also flood the DB.
_DEGRADED_AUDIT_INTERVAL_SECONDS = 60.0
_last_degraded_audit: dict[str, float] = {}


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiterUnavailable(Exception):
    """Redis could not be reached and the caller asked to fail closed."""


# One script = one atomic step on the Redis server: increment, and if the key is new (or has
# somehow lost its TTL) give it one. Returns {count, seconds-left}.
_ATOMIC_INCR = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""

_KEY_PREFIX = "ratelimit:"


def _atomic_incr(redis_client: redis.Redis, redis_key: str, window_seconds: int) -> tuple[int, int]:
    count, ttl = redis_client.eval(_ATOMIC_INCR, 1, redis_key, window_seconds)  # type: ignore[misc]
    return int(count), int(ttl)


def check_rate_limit(
    redis_client: redis.Redis,
    *,
    key: str,
    limit: int,
    window_seconds: int,
    fail_closed: bool = False,
) -> bool:
    """Raises RateLimitExceeded once `key` has been hit more than `limit` times in
    the current fixed window. On a Redis error: raises RateLimiterUnavailable if
    `fail_closed`, otherwise fails open and returns True ("degraded"). Returns
    False in the normal healthy case."""
    try:
        count, ttl = _atomic_incr(redis_client, f"{_KEY_PREFIX}{key}", window_seconds)
    except redis.RedisError as exc:
        if fail_closed:
            raise RateLimiterUnavailable from exc
        return True
    if count <= limit:
        return False
    raise RateLimitExceeded(f"Rate limit exceeded for {key}", retry_after=max(ttl, 1))


def record_failure(redis_client: redis.Redis, *, key: str, window_seconds: int) -> tuple[int, int]:
    """Count one FAILED attempt against `key` (atomic, always with a TTL). Returns
    (failures in this window, seconds left). Raises redis.RedisError."""
    return _atomic_incr(redis_client, f"{_KEY_PREFIX}{key}", window_seconds)


def failure_state(redis_client: redis.Redis, *, key: str) -> tuple[int, int]:
    """(failures so far, seconds left) WITHOUT counting anything. Raises redis.RedisError."""
    redis_key = f"{_KEY_PREFIX}{key}"
    raw = redis_client.get(redis_key)
    if raw is None:
        return 0, 0
    ttl = int(redis_client.ttl(redis_key))  # type: ignore[arg-type]
    return int(raw), ttl  # type: ignore[arg-type]


def _audit_degraded(db: Session, key_prefix: str) -> None:
    now = time.monotonic()
    if now - _last_degraded_audit.get(key_prefix, -_DEGRADED_AUDIT_INTERVAL_SECONDS) < (
        _DEGRADED_AUDIT_INTERVAL_SECONDS
    ):
        return
    _last_degraded_audit[key_prefix] = now
    try:
        record_audit_event(
            db,
            actor="rate-limiter",
            action="rate_limit.unavailable_failed_open",
            entity_type="system",
            entity_id=None,
            metadata={"endpoint": key_prefix},
        )
    except SQLAlchemyError:
        # The audit write must never take down the endpoint it is reporting on; the
        # per-request log line above is the fallback record.
        logger.exception("rate_limit_degraded_audit_failed", extra={"endpoint": key_prefix})


def client_ip_identity(request: Request) -> str:
    """Default identity before authentication: the client address from the ONE shared helper
    (`app.core.client_address`) -- the socket peer, or the forwarded client only when the peer
    is a configured trusted proxy. X-Forwarded-For is never trusted on its own."""
    return rate_limit_identity(request)


def rate_limit_dependency(
    *,
    limit: int | Callable[[], int],
    window_seconds: int,
    key_prefix: str,
    fail_closed: bool = False,
    identity: Callable[..., str] = client_ip_identity,
):
    """FastAPI dependency factory. `identity` is itself a FastAPI dependency that
    returns the bucket owner: the client IP by default, or (authenticated routes)
    the verified principal -- see `api.admin_deps.principal_identity`. Keying on the
    principal means rotating IPs / forwarded headers does not reset the budget, and
    an identity dependency that fails authentication (401) runs before any counting.
    `limit` may be a callable so it is read per request (settings stay tunable)."""

    def _dependency(
        db: Session = Depends(get_db), bucket_owner: str = Depends(identity)
    ) -> None:
        from app.core.redis_client import get_redis

        effective_limit = limit() if callable(limit) else limit
        try:
            degraded = check_rate_limit(
                get_redis(),
                key=f"{key_prefix}:{bucket_owner}",
                limit=effective_limit,
                window_seconds=window_seconds,
                fail_closed=fail_closed,
            )
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except RateLimiterUnavailable as exc:
            logger.error("rate_limiter_unavailable_failing_closed", extra={"endpoint": key_prefix})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable.",
            ) from exc
        if degraded:
            logger.error("rate_limiter_unavailable_failing_open", extra={"endpoint": key_prefix})
            _audit_degraded(db, key_prefix)

    return _dependency


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please try again later.",
        headers={"Retry-After": str(max(retry_after, 1))},
    )


# --- webhooks: verify the secret first, then limit --------------------------------------

_WINDOW_SECONDS = 60


def enforce_valid_webhook_ceiling(db: Session, request: Request, *, key_prefix: str) -> None:
    """A request that carried the VALID secret. Only a generous per-address ceiling applies
    (`webhook_valid_ceiling_per_minute`); failed-authentication counters are never consulted,
    so an attacker flooding bad requests from this address cannot throttle the real caller.
    Fails open (audited) if Redis is down: dropping a legitimate completion webhook would lose
    a call result for good."""
    from app.core.redis_client import get_redis

    try:
        degraded = check_rate_limit(
            get_redis(),
            key=f"{key_prefix}:{client_ip_identity(request)}",
            limit=get_settings().webhook_valid_ceiling_per_minute,
            window_seconds=_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        raise _too_many(exc.retry_after) from exc
    if degraded:
        logger.error("rate_limiter_unavailable_failing_open", extra={"endpoint": key_prefix})
        _audit_degraded(db, key_prefix)


def throttle_failed_webhook_auth(request: Request, *, key_prefix: str) -> None:
    """A request that FAILED authentication. Counts it against this client address; the caller
    answers 401 unless this raises 429 because the address is now over
    `webhook_rate_limit_per_minute` failures in the window. Redis down -> fail open (the
    caller still answers 401: authentication itself never fails open)."""
    from app.core.redis_client import get_redis

    identity = client_ip_identity(request)
    try:
        failures, ttl = record_failure(
            get_redis(), key=f"{key_prefix}_authfail:{identity}", window_seconds=_WINDOW_SECONDS
        )
    except redis.RedisError:
        logger.error("rate_limiter_unavailable_failing_open", extra={"endpoint": key_prefix})
        return
    if failures > get_settings().webhook_rate_limit_per_minute:
        raise _too_many(ttl)


# --- login: count FAILED attempts only ---------------------------------------------------


@dataclass(frozen=True)
class FailureBucket:
    key: str  # without the "ratelimit:" prefix
    limit: int  # failures allowed per window; the next attempt is refused


def enforce_failure_buckets(buckets: Sequence[FailureBucket], *, endpoint: str) -> None:
    """Refuse the attempt (429) if ANY bucket already holds `limit` failures. Fails CLOSED
    (503) when Redis cannot be read: an unthrottled login is a brute-force oracle, so even a
    correct credential gets no token while the limiter is blind."""
    from app.core.redis_client import get_redis

    worst_ttl = 0
    over = False
    try:
        client = get_redis()
        for bucket in buckets:
            failures, ttl = failure_state(client, key=bucket.key)
            if failures >= bucket.limit:
                over = True
                worst_ttl = max(worst_ttl, ttl)
    except redis.RedisError as exc:
        logger.error("rate_limiter_unavailable_failing_closed", extra={"endpoint": endpoint})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable.",
        ) from exc
    if over:
        raise _too_many(worst_ttl)


def record_failure_buckets(buckets: Sequence[FailureBucket], *, endpoint: str) -> None:
    """Count one failed attempt in every bucket. A Redis error here is logged, not raised: the
    attempt has already failed, and the pre-check above is what fails closed."""
    from app.core.redis_client import get_redis

    try:
        client = get_redis()
        for bucket in buckets:
            record_failure(client, key=bucket.key, window_seconds=_WINDOW_SECONDS)
    except redis.RedisError:
        logger.error("rate_limiter_record_failure_failed", extra={"endpoint": endpoint})
