"""Minimal, multi-worker-safe circuit breaker for provider health --
Checkpoint 03 Step 20. Redis-backed (not an in-process variable, which
wouldn't be shared across worker processes -- Step 23's "do not rely on
an in-memory Python variable" applies here too, not just concurrency
counters).

Concept: consecutive provider failures increment a counter; hitting the
threshold opens the circuit for `open_seconds` (a Redis key with a TTL
-- expiry *is* the recovery/probe mechanism: once the key expires the
next call is allowed through as a live probe, and its own outcome
decides whether the circuit stays closed or reopens).
"""

import redis

from app.core.config import get_settings


class CircuitBreaker:
    def __init__(self, redis_client: redis.Redis, provider_name: str) -> None:
        self.redis = redis_client
        self.provider_name = provider_name
        settings = get_settings()
        self.error_threshold = settings.circuit_breaker_error_threshold
        self.open_seconds = settings.circuit_breaker_open_seconds

    def _errors_key(self) -> str:
        return f"circuit:{self.provider_name}:errors"

    def _open_key(self) -> str:
        return f"circuit:{self.provider_name}:open"

    def is_open(self) -> bool:
        return bool(self.redis.exists(self._open_key()))

    def record_success(self) -> None:
        self.redis.delete(self._errors_key())

    def record_failure(self) -> None:
        errors: int = self.redis.incr(self._errors_key())  # type: ignore[assignment]
        if errors >= self.error_threshold:
            self.redis.set(self._open_key(), "1", ex=self.open_seconds)
