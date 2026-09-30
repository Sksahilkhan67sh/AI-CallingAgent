"""Fixed-window per-client rate limiting -- Checkpoint 09.

Redis counters keyed by (name, client IP, minute). Applied only to
externally reachable HTTP endpoints (admin login, webhook) -- never to
internal worker operations. If Redis is unavailable the limiter fails OPEN
(and logs): authentication/secret checks still apply, and taking login
down with Redis would be the worse failure.
"""

import logging
import time
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis_client import get_redis

logger = logging.getLogger("rate_limit")


def rate_limit(name: str, limit_attr: str) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        limit: int = getattr(get_settings(), limit_attr)
        client = request.client.host if request.client else "unknown"
        key = f"ratelimit:{name}:{client}:{int(time.time() // 60)}"
        try:
            redis = get_redis()
            count: int = redis.incr(key)  # type: ignore[assignment]
            if count == 1:
                redis.expire(key, 70)
        except Exception:
            logger.warning("rate_limit_unavailable", extra={"limiter": name})
            return
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": "60"},
            )

    return dependency
