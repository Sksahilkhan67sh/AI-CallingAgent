"""Lightweight fixed-window rate limiter -- Checkpoint 09 §8.5, hardened in CP11.

Redis-backed (rate limiting is an explicitly appropriate use of Redis, never
permanent business state). Deliberately simple -- one INCR + conditional EXPIRE
per window, no new dependency, no token/leaky-bucket abstraction.

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
from collections.abc import Callable

import redis
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
    redis_key = f"ratelimit:{key}"
    try:
        count: int = redis_client.incr(redis_key)  # type: ignore[assignment]
        if count == 1:
            redis_client.expire(redis_key, window_seconds)
        if count <= limit:
            return False
        # Over the limit. INCR and EXPIRE are two commands: a process dying between
        # them would leave a key with no TTL -- a PERMANENT block on this caller.
        # Heal it here (only the blocked path pays for the extra round trip).
        ttl: int = redis_client.ttl(redis_key)  # type: ignore[assignment]
        if ttl < 0:
            redis_client.expire(redis_key, window_seconds)
            ttl = window_seconds
    except redis.RedisError as exc:
        if fail_closed:
            raise RateLimiterUnavailable from exc
        return True
    raise RateLimitExceeded(f"Rate limit exceeded for {key}", retry_after=max(ttl, 1))


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
    """Default identity before authentication (login, webhooks): the peer address.

    `request.client.host`; behind a reverse proxy run uvicorn with
    `--proxy-headers --forwarded-allow-ips=<proxy>` so that is the real client.
    X-Forwarded-For is deliberately NOT parsed here -- it is caller-controlled."""
    return f"ip:{request.client.host if request.client else 'unknown'}"


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
