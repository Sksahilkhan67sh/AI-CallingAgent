"""Redis connection management -- one connection pool per process,
mirroring app/core/database.py's "one engine, not one per request"
rule. Redis here is used only for queue/hot distributed state (Streams,
counters, locks) -- PostgreSQL remains the durable source of truth for
everything business-meaningful (Checkpoint 03 non-negotiable)."""

from functools import lru_cache

import redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    settings = get_settings()
    # P1-7: explicit timeouts. Without them a stalled Redis (a dropped connection that never
    # resets, a frozen process) blocks the calling thread for ever -- and every worker, the
    # API and the sweeper share this client.
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=settings.redis_health_check_interval_seconds,
    )
