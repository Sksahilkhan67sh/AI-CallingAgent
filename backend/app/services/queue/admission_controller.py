"""Admission control -- Checkpoint 03 Steps 8-11, 20.

Redis-backed so limits are enforced correctly across multiple worker
processes (Step 23: never an in-process variable). Two kinds of limit,
checked in this order for every admission attempt:

1. Circuit breaker -- if the provider's circuit is open, reject
   immediately without touching CPS/concurrency state.
2. Concurrency (a *reservation*, held until `release()`): global,
   campaign, and provider counters are all incremented; if any exceeds
   its limit, every increment made so far is rolled back and the
   attempt is rejected as a whole -- partial reservations are never
   left behind.
3. CPS (a rolling per-second counter, not a reservation): only counted
   once a call is actually going to be admitted, so CPS measures actual
   dial attempts, not rejected ones. If any CPS counter is over limit,
   the CPS increments *and* the concurrency reservation from step 2 are
   both rolled back.

A caller that receives `admitted=True` MUST call `release()` once the
call's outcome is persisted -- concurrency slots are held until then.
"""

import time
from dataclasses import dataclass

import redis

from app.services.telephony.circuit_breaker import CircuitBreaker


@dataclass
class AdmissionResult:
    admitted: bool
    reason: str | None = None


class AdmissionController:
    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        global_cps_limit: int,
        campaign_cps_limit: int,
        provider_cps_limit: int,
        global_concurrency_limit: int,
        campaign_concurrency_limit: int,
        provider_concurrency_limit: int,
    ) -> None:
        self.redis = redis_client
        self.global_cps_limit = global_cps_limit
        self.campaign_cps_limit = campaign_cps_limit
        self.provider_cps_limit = provider_cps_limit
        self.global_concurrency_limit = global_concurrency_limit
        self.campaign_concurrency_limit = campaign_concurrency_limit
        self.provider_concurrency_limit = provider_concurrency_limit

    def try_admit(self, *, campaign_id: str, provider_name: str) -> AdmissionResult:
        breaker = CircuitBreaker(self.redis, provider_name)
        if breaker.is_open():
            return AdmissionResult(False, "circuit_open")

        concurrency_keys = [
            ("concurrency:global", self.global_concurrency_limit),
            (f"concurrency:campaign:{campaign_id}", self.campaign_concurrency_limit),
            (f"concurrency:provider:{provider_name}", self.provider_concurrency_limit),
        ]
        incremented: list[str] = []
        for key, limit in concurrency_keys:
            value: int = self.redis.incr(key)  # type: ignore[assignment]
            incremented.append(key)
            if value > limit:
                self._rollback(incremented)
                return AdmissionResult(False, "concurrency_exceeded")

        cps_keys = self._cps_keys(campaign_id, provider_name)
        cps_incremented: list[str] = []
        for key, limit in cps_keys:
            value = self.redis.incr(key)  # type: ignore[assignment]
            if value == 1:
                self.redis.expire(key, 2)  # small buffer over the 1s window
            cps_incremented.append(key)
            if value > limit:
                self._rollback(cps_incremented)
                self._rollback(incremented)
                return AdmissionResult(False, "cps_exceeded")

        return AdmissionResult(True)

    def release(self, *, campaign_id: str, provider_name: str) -> None:
        self._rollback(
            [
                "concurrency:global",
                f"concurrency:campaign:{campaign_id}",
                f"concurrency:provider:{provider_name}",
            ]
        )

    def _cps_keys(self, campaign_id: str, provider_name: str) -> list[tuple[str, int]]:
        second = int(time.time())
        return [
            (f"cps:global:{second}", self.global_cps_limit),
            (f"cps:campaign:{campaign_id}:{second}", self.campaign_cps_limit),
            (f"cps:provider:{provider_name}:{second}", self.provider_cps_limit),
        ]

    def _rollback(self, keys: list[str]) -> None:
        for key in keys:
            self.redis.decr(key)
