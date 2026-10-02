"""Lightweight fixed-window rate limiter -- Checkpoint 09 §8.5.

Redis-backed (per §12: rate limiting is an explicitly appropriate use
of Redis, never permanent business state). Deliberately simple -- a
single INCR + conditional EXPIRE per window, no new dependency, no
token-bucket/leaky-bucket abstraction. Scoped to public-facing
endpoints only (admin login, the Dograh webhook); internal worker loops
never call this.
"""

import redis
from fastapi import HTTPException, Request, status


class RateLimitExceeded(Exception):
    pass


def check_rate_limit(
    redis_client: redis.Redis, *, key: str, limit: int, window_seconds: int
) -> None:
    """Raises RateLimitExceeded once `key` has been hit more than
    `limit` times within the current fixed window. Fails open on a
    Redis error -- an unreachable rate limiter must never itself take
    down the endpoint it's protecting (§12: handle Redis outage
    safely)."""
    try:
        redis_key = f"ratelimit:{key}"
        count: int = redis_client.incr(redis_key)  # type: ignore[assignment]
        if count == 1:
            redis_client.expire(redis_key, window_seconds)
    except redis.RedisError:
        return
    if count > limit:
        raise RateLimitExceeded(f"Rate limit exceeded for {key}")


def rate_limit_dependency(*, limit: int, window_seconds: int, key_prefix: str):
    """FastAPI dependency factory -- keys on the caller's IP address,
    the best identity available for an unauthenticated public endpoint
    (login, webhook-before-auth-is-checked)."""

    def _dependency(request: Request) -> None:
        from app.core.redis_client import get_redis

        client_ip = request.client.host if request.client else "unknown"
        try:
            check_rate_limit(
                get_redis(),
                key=f"{key_prefix}:{client_ip}",
                limit=limit,
                window_seconds=window_seconds,
            )
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            ) from exc

    return _dependency
