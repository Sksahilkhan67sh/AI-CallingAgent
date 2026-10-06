"""Concurrency leases -- Checkpoint 12-A (C2: concurrency slot leaks).

Until CP12-A a concurrency slot was a bare Redis counter: INCR on admission,
DECR in a worker `finally`. A worker killed between the two left the counter
permanently high (no TTL, no owner), and a late DECR from a stale worker
could free a slot that a live call was still using.

A *lease* replaces the counter. Per scope (global / campaign / provider) the
active leases are a sorted set, member = lease id, score = expiry (epoch ms).
Capacity is the number of members whose expiry is still in the future.

* Acquire / renew / release are single Lua scripts, so each is atomic across
  all three scopes -- there is no partial acquisition to roll back, and no
  window between "check capacity" and "record ownership".
* Expired members are dropped inside the acquire script itself
  (ZREMRANGEBYSCORE touches only the expired prefix, never an O(N) scan), so a
  crashed worker's slot returns to the pool without any janitor process.
* The lease id is a random token only the acquiring worker holds. Release and
  renew act on that exact member, so a stale worker can never free or extend a
  newer owner's lease, and a repeated release is a no-op.
* One extra "holder" key per logical attempt (`holder_id`) makes acquisition
  idempotent: a second acquire for the same attempt while its lease is live is
  rejected instead of consuming another slot.

Time comes from Redis (`TIME`) inside the scripts, so worker clock skew cannot
shorten or extend a lease. Tests inject `clock` to move time deterministically.

Redis stays hot-state only: nothing here is durable, and PostgreSQL's
CallAttempt remains the source of truth for the call itself.
"""

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

import redis
from redis.commands.core import Script

logger = logging.getLogger("concurrency_lease")

_SCOPE_KEY_PREFIX = "concurrency:lease"
_HOLDER_KEY_PREFIX = "concurrency:holder"

# Rejection reasons returned by the acquire script, in check order.
REASON_DUPLICATE = "duplicate_lease"
REASON_GLOBAL = "global"
REASON_CAMPAIGN = "campaign"
REASON_PROVIDER = "provider"

# KEYS: global, campaign, provider scope sets, then the holder key.
# ARGV: now_ms ("" -> Redis TIME), ttl_ms, lease_id, acquired_by, global/campaign/provider limit.
# Returns {acquired(1/0), rejection reason or "", expired leases reclaimed}.
_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
if now == nil then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local ttl = tonumber(ARGV[2])
local expires = now + ttl

local reclaimed = 0
for i = 1, 3 do
  reclaimed = reclaimed + redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
end

local holder = redis.call('GET', KEYS[4])
if holder then
  local live_until = tonumber(string.match(holder, '^[^|]*|([^|]*)'))
  if live_until ~= nil and live_until > now then
    return {0, 'duplicate_lease', reclaimed}
  end
end

local names = {'global', 'campaign', 'provider'}
for i = 1, 3 do
  if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[4 + i]) then
    return {0, names[i], reclaimed}
  end
end

for i = 1, 3 do
  redis.call('ZADD', KEYS[i], expires, ARGV[3])
  redis.call('PEXPIRE', KEYS[i], ttl)
end
redis.call('SET', KEYS[4], ARGV[3] .. '|' .. expires .. '|' .. now .. '|' .. ARGV[4], 'PX', ttl)
return {1, '', reclaimed}
"""

# ARGV: now_ms ("" -> Redis TIME), ttl_ms, lease_id. Returns 1 renewed, 0 lease not live.
# ZADD XX only updates an existing member: a renewal can never recreate a lease that was
# released, expired or reclaimed -- all three scopes must still hold it, unexpired.
_RENEW_LUA = """
local now = tonumber(ARGV[1])
if now == nil then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local ttl = tonumber(ARGV[2])
local expires = now + ttl

for i = 1, 3 do
  local score = redis.call('ZSCORE', KEYS[i], ARGV[3])
  if not score or tonumber(score) <= now then
    return 0
  end
end

for i = 1, 3 do
  redis.call('ZADD', KEYS[i], 'XX', expires, ARGV[3])
  redis.call('PEXPIRE', KEYS[i], ttl)
end

local holder = redis.call('GET', KEYS[4])
if holder and string.sub(holder, 1, #ARGV[3] + 1) == ARGV[3] .. '|' then
  local fields = {}
  for part in string.gmatch(holder, '[^|]+') do
    fields[#fields + 1] = part
  end
  redis.call('SET', KEYS[4],
    ARGV[3] .. '|' .. expires .. '|' .. (fields[3] or '') .. '|' .. (fields[4] or ''), 'PX', ttl)
end
return 1
"""

# ARGV: lease_id. Returns how many scope sets still held it (0 = already released / expired
# and reclaimed / never ours). The holder key is only deleted if it still names this lease,
# so a stale release cannot clear a newer lease's idempotency marker.
_RELEASE_LUA = """
local removed = 0
for i = 1, 3 do
  removed = removed + redis.call('ZREM', KEYS[i], ARGV[1])
end
local holder = redis.call('GET', KEYS[4])
if holder and string.sub(holder, 1, #ARGV[1] + 1) == ARGV[1] .. '|' then
  redis.call('DEL', KEYS[4])
end
return removed
"""


@dataclass(frozen=True)
class ConcurrencyLease:
    """Proof of ownership of one concurrency slot in every scope.

    `lease_id` is the only credential: whoever holds it can release or renew,
    nobody else can. Lease state lives in Redis; this object is just the handle.
    """

    lease_id: str
    holder_id: str
    campaign_id: str
    provider_name: str


@dataclass(frozen=True)
class AcquireOutcome:
    lease: ConcurrencyLease | None
    reason: str | None
    reclaimed: int


class ConcurrencyLeaseStore:
    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        ttl_seconds: int,
        global_limit: int,
        campaign_limit: int,
        provider_limit: int,
        worker_id: str,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be > 0: a lease must always expire")
        self._redis = redis_client
        self._ttl_ms = ttl_seconds * 1000
        self._limits = (global_limit, campaign_limit, provider_limit)
        # worker_id is stored in the holder record for operators; '|' is the field separator.
        self._worker_id = worker_id.replace("|", "_")
        self._clock = clock

    # Scripts are registered on first use, not in __init__: the factory builds a controller per
    # call, and constructing one must not touch Redis. redis-py's Script re-loads itself
    # transparently after a Redis restart / SCRIPT FLUSH (NOSCRIPT -> EVAL).
    @cached_property
    def _acquire(self) -> Script:
        return self._redis.register_script(_ACQUIRE_LUA)

    @cached_property
    def _renew(self) -> Script:
        return self._redis.register_script(_RENEW_LUA)

    @cached_property
    def _release(self) -> Script:
        return self._redis.register_script(_RELEASE_LUA)

    @staticmethod
    def scope_keys(campaign_id: str, provider_name: str) -> list[str]:
        return [
            f"{_SCOPE_KEY_PREFIX}:global",
            f"{_SCOPE_KEY_PREFIX}:campaign:{campaign_id}",
            f"{_SCOPE_KEY_PREFIX}:provider:{provider_name}",
        ]

    def _keys(self, campaign_id: str, provider_name: str, holder_id: str) -> list[str]:
        return [*self.scope_keys(campaign_id, provider_name), f"{_HOLDER_KEY_PREFIX}:{holder_id}"]

    def _now_arg(self) -> str:
        return "" if self._clock is None else str(self._clock())

    def acquire(self, *, campaign_id: str, provider_name: str, holder_id: str) -> AcquireOutcome:
        """Atomically take one slot in every scope, or none. Raises redis.RedisError if
        Redis cannot answer -- the caller decides the failure policy (admission fails closed)."""
        lease_id = secrets.token_hex(16)
        keys = self._keys(campaign_id, provider_name, holder_id)
        try:
            acquired, reason, reclaimed = self._acquire(
                keys=keys,
                args=[self._now_arg(), self._ttl_ms, lease_id, self._worker_id, *self._limits],
            )
        except redis.RedisError:
            # The script may have run although its reply was lost. Releasing this exact lease id
            # is safe either way (it removes only our own member); if Redis is still down the
            # TTL reclaims it.
            self._release_best_effort(keys, lease_id)
            raise
        if reclaimed:
            logger.warning(
                "lease_stale_reclaimed",
                extra={
                    "count": int(reclaimed),
                    "holder_id": holder_id,
                    "worker_id": self._worker_id,
                },
            )
        if not acquired:
            logger.info(
                "lease_acquisition_rejected",
                extra={"reason": reason, "holder_id": holder_id, "campaign_id": campaign_id},
            )
            return AcquireOutcome(None, str(reason), int(reclaimed))
        logger.info(
            "lease_acquired",
            extra={
                "lease_id": lease_id,
                "holder_id": holder_id,
                "campaign_id": campaign_id,
                "worker_id": self._worker_id,
            },
        )
        lease = ConcurrencyLease(lease_id, holder_id, campaign_id, provider_name)
        return AcquireOutcome(lease, None, int(reclaimed))

    def _release_best_effort(self, keys: list[str], lease_id: str) -> None:
        try:
            self._release(keys=keys, args=[lease_id])
        except redis.RedisError as exc:
            logger.error(
                "lease_redis_operation_failed",
                extra={
                    "operation": "release_after_failed_acquire",
                    "lease_id": lease_id,
                    "error_type": type(exc).__name__,
                },
            )

    def renew(self, lease: ConcurrencyLease) -> bool:
        """Extend a still-live lease by one TTL. False means the lease is gone (released,
        expired or reclaimed) -- it is never recreated. Raises redis.RedisError on Redis failure."""
        renewed = self._renew(
            keys=self._keys(lease.campaign_id, lease.provider_name, lease.holder_id),
            args=[self._now_arg(), self._ttl_ms, lease.lease_id],
        )
        if renewed:
            logger.info(
                "lease_renewed",
                extra={"lease_id": lease.lease_id, "holder_id": lease.holder_id},
            )
            return True
        logger.warning(
            "lease_ownership_mismatch",
            extra={
                "operation": "renew",
                "lease_id": lease.lease_id,
                "holder_id": lease.holder_id,
            },
        )
        return False

    def release(self, lease: ConcurrencyLease) -> bool:
        """Free this lease's slot. Idempotent; a no-op (False) if the lease is already gone,
        in particular after expiry -- it can never free a different owner's lease.
        Raises redis.RedisError on Redis failure."""
        removed = self._release(
            keys=self._keys(lease.campaign_id, lease.provider_name, lease.holder_id),
            args=[lease.lease_id],
        )
        if removed:
            logger.info(
                "lease_released",
                extra={"lease_id": lease.lease_id, "holder_id": lease.holder_id},
            )
            return True
        logger.warning(
            "lease_ownership_mismatch",
            extra={
                "operation": "release",
                "lease_id": lease.lease_id,
                "holder_id": lease.holder_id,
            },
        )
        return False
