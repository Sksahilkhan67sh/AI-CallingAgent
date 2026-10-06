"""Admission control -- Checkpoint 03 Steps 8-11, 20; concurrency hardened in CP12-A.

Redis-backed so limits are enforced correctly across multiple worker
processes (Step 23: never an in-process variable). Two kinds of limit,
checked in this order for every admission attempt:

1. Circuit breaker -- if the provider's circuit is open, reject
   immediately without touching CPS/concurrency state.
2. Concurrency (a *lease*, held until `release()` or until it expires):
   one slot in each of the global, campaign and provider scopes is taken
   by a single atomic Redis script -- all three or none, so a partial
   reservation can never be left behind. See concurrency_lease.py for why
   this is a lease with an owner and a TTL rather than a counter.
3. CPS (a rolling per-second counter, not a reservation): only counted
   once a call is actually going to be admitted, so CPS measures actual
   dial attempts, not rejected ones. If any CPS counter is over limit,
   the CPS increments *and* the lease from step 2 are both rolled back.

Failure policy: if Redis cannot be reached or answers with an error, no call
is admitted -- the `redis.RedisError` propagates (logged first), the worker
leaves the job unacked, and a lease taken before the failure is released (or,
if Redis is still down, left to its TTL). A Redis outage must never turn into
uncontrolled outbound dialing, and there is deliberately no in-process
fallback counter.

A caller that receives `admitted=True` MUST call `release(result.lease)`
once the call's outcome is persisted. If it never does (worker killed), the
lease expires on its own after `concurrency_lease_ttl_seconds`.
"""

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import redis

from app.core.config import get_settings
from app.services.queue.concurrency_lease import (
    REASON_DUPLICATE,
    ConcurrencyLease,
    ConcurrencyLeaseStore,
)
from app.services.telephony.circuit_breaker import CircuitBreaker

logger = logging.getLogger("admission_controller")


@dataclass
class AdmissionResult:
    admitted: bool
    reason: str | None = None
    lease: ConcurrencyLease | None = None


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
        lease_ttl_seconds: int | None = None,
        worker_id: str | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.redis = redis_client
        self.global_cps_limit = global_cps_limit
        self.campaign_cps_limit = campaign_cps_limit
        self.provider_cps_limit = provider_cps_limit
        self.global_concurrency_limit = global_concurrency_limit
        self.campaign_concurrency_limit = campaign_concurrency_limit
        self.provider_concurrency_limit = provider_concurrency_limit
        self.leases = ConcurrencyLeaseStore(
            redis_client,
            ttl_seconds=(
                lease_ttl_seconds
                if lease_ttl_seconds is not None
                else get_settings().concurrency_lease_ttl_seconds
            ),
            global_limit=global_concurrency_limit,
            campaign_limit=campaign_concurrency_limit,
            provider_limit=provider_concurrency_limit,
            worker_id=worker_id or f"{socket.gethostname()}-{os.getpid()}",
            clock=clock,
        )

    def try_admit(
        self, *, campaign_id: str, provider_name: str, holder_id: str | None = None
    ) -> AdmissionResult:
        """`holder_id` identifies the logical attempt (the job's idempotency key): a second
        admission for the same holder while its lease is live is rejected ("duplicate_lease"),
        so a duplicate delivery cannot consume two slots. Omitted -> a fresh id per call."""
        holder = holder_id or uuid.uuid4().hex
        try:
            return self._admit(campaign_id, provider_name, holder)
        except redis.RedisError as exc:
            logger.error(
                "lease_redis_operation_failed",
                extra={
                    "operation": "admit",
                    "holder_id": holder,
                    "error_type": type(exc).__name__,
                },
            )
            raise

    def _admit(self, campaign_id: str, provider_name: str, holder_id: str) -> AdmissionResult:
        if CircuitBreaker(self.redis, provider_name).is_open():
            return AdmissionResult(False, "circuit_open")

        outcome = self.leases.acquire(
            campaign_id=campaign_id, provider_name=provider_name, holder_id=holder_id
        )
        lease = outcome.lease
        if lease is None:
            duplicate = outcome.reason == REASON_DUPLICATE
            return AdmissionResult(False, REASON_DUPLICATE if duplicate else "concurrency_exceeded")

        cps_incremented: list[str] = []
        try:
            for key, limit in self._cps_keys(campaign_id, provider_name):
                value: int = self.redis.incr(key)  # type: ignore[assignment]
                if value == 1:
                    self.redis.expire(key, 2)  # small buffer over the 1s window
                cps_incremented.append(key)
                if value > limit:
                    self._rollback(cps_incremented)
                    self.release(lease)
                    return AdmissionResult(False, "cps_exceeded")
        except redis.RedisError:
            self.release(lease)  # never leave the slot held for a call that will not be placed
            raise

        return AdmissionResult(True, lease=lease)

    def release(self, lease: ConcurrencyLease) -> bool:
        """Ownership-safe and idempotent: frees only `lease` itself. Never raises -- it runs
        in `finally` blocks and must not mask the real outcome; if Redis is down the lease
        simply expires on its TTL, which is the final safety net."""
        try:
            return self.leases.release(lease)
        except redis.RedisError as exc:
            logger.error(
                "lease_redis_operation_failed",
                extra={
                    "operation": "release",
                    "lease_id": lease.lease_id,
                    "holder_id": lease.holder_id,
                    "error_type": type(exc).__name__,
                },
            )
            return False

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
