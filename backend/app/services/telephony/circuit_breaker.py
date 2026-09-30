"""Multi-worker-safe circuit breaker for provider health -- Checkpoints 03, 09.

States (all in Redis so every worker sees the same state):

  CLOSED    -- `errors` counter (TTL = failure window) below threshold.
  OPEN      -- `open` key present (TTL = cooldown). All requests refused.
  HALF_OPEN -- cooldown expired but `tripped` marker still set. Exactly one
               probe at a time is let through (`probe` key, SET NX). A probe
               success closes the circuit; a probe failure reopens it for a
               full cooldown, so a still-down provider sees one request per
               cooldown rather than a retry storm.
"""

import enum
import logging

import redis

from app.core.config import get_settings
from app.core.metrics import incr

logger = logging.getLogger("circuit_breaker")


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, redis_client: redis.Redis, provider_name: str) -> None:
        self.redis = redis_client
        self.provider_name = provider_name
        settings = get_settings()
        self.error_threshold = settings.circuit_breaker_error_threshold
        self.open_seconds = settings.circuit_breaker_open_seconds
        self.window_seconds = settings.circuit_breaker_window_seconds

    def _key(self, suffix: str) -> str:
        return f"circuit:{self.provider_name}:{suffix}"

    def state(self) -> CircuitState:
        if self.redis.exists(self._key("open")):
            return CircuitState.OPEN
        if self.redis.exists(self._key("tripped")):
            return CircuitState.HALF_OPEN
        return CircuitState.CLOSED

    def is_open(self) -> bool:
        return self.state() == CircuitState.OPEN

    def allow_request(self) -> bool:
        state = self.state()
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.OPEN:
            return False
        return bool(self.redis.set(self._key("probe"), "1", nx=True, ex=self.open_seconds))

    def record_success(self) -> None:
        was_tripped = self.redis.exists(self._key("tripped"))
        self.redis.delete(
            self._key("errors"), self._key("open"), self._key("tripped"), self._key("probe")
        )
        if was_tripped:
            logger.info("circuit_closed", extra={"provider": self.provider_name})

    def record_failure(self) -> None:
        if self.state() == CircuitState.HALF_OPEN:
            self._open()  # failed probe: reopen for a full cooldown
            return
        errors: int = self.redis.incr(self._key("errors"))  # type: ignore[assignment]
        if errors == 1:
            self.redis.expire(self._key("errors"), self.window_seconds)
        if errors >= self.error_threshold:
            self._open()

    def _open(self) -> None:
        self.redis.set(self._key("open"), "1", ex=self.open_seconds)
        self.redis.set(self._key("tripped"), "1")
        self.redis.delete(self._key("errors"), self._key("probe"))
        incr("circuit_opened")
        logger.warning("circuit_opened", extra={"provider": self.provider_name})
