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
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)
