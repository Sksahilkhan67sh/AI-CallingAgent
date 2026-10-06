# Checkpoint 12-A — Concurrency Lease (C2: slot leaks)

## 1. Audit of the pre-CP12 flow

```
dequeue -> kill switch -> admission (INCR x3) -> _dial -> persist attempt -> release (DECR x3) -> ack
```

* `AdmissionController.try_admit` incremented three plain counters
  (`concurrency:global`, `:campaign:<id>`, `:provider:<name>`) and rolled back with `DECR` if a
  later counter was over its limit. The increment/check/rollback was not atomic.
* The only release was `admission.release(...)` in a `finally` in `process_claimed_job`.
* Counters had no TTL and no owner. No lease/TTL mechanism existed; no cleanup code touched them.
* The slot is taken **before** the `CallAttempt` row exists, so it cannot be keyed by `attempt_id`.

Ways a slot leaked or was freed wrongly:

1. Worker killed / OOM / container stop between `INCR` and `DECR` — slot lost forever.
2. Redis error between the three `INCR`s, or inside the rollback — partial counts left behind.
3. A late `DECR` from a stale worker freed a slot a live call was using (no ownership).
4. A Redis flush/restart reset counters to zero while calls were in flight (inverse error).

## 2. What a concurrency lease is

A slot held by an owner until released **or until it expires**. Per scope, active leases are a Redis
sorted set: member = lease id, score = expiry (epoch ms). Capacity used = members with expiry in the
future. A counter cannot say *who* holds a slot or *when* it should be given up, so it cannot recover
from a dead owner; a lease can.

Keys: `concurrency:lease:global`, `concurrency:lease:campaign:<id>`, `concurrency:lease:provider:<name>`
(new prefix on purpose — old integer keys would raise WRONGTYPE), plus `concurrency:holder:<holder_id>`
= `lease_id|expires_ms|acquired_ms|worker_id` (diagnostics + idempotency).
Code: `app/services/queue/concurrency_lease.py`; entry point is still `AdmissionController`.

## 3. Lifecycle

* **Acquire** (one Lua script, all scopes): drop expired members → reject if the holder already has a
  live lease (`duplicate_lease`) → reject if any scope is full → otherwise add to all three scopes and
  write the holder record. Nothing is written on rejection, so there is nothing to roll back.
  Hot-path cost: O(log N) per scope plus the number of *expired* members removed.
* **Release** (one script): removes exactly this lease id from each scope; clears the holder record only
  if it still names this lease. Idempotent; never raises from `AdmissionController.release`.
* **Renew** (one script): succeeds only if the lease is present *and unexpired* in all three scopes,
  then extends it by one TTL. It never creates a lease. An expired lease is dead even if nobody has
  cleaned it up yet.
* `holder_id` is the job's idempotency key (campaign:contact:attempt_number) — one logical attempt, one slot.

## 4. TTL and renewal

`CONCURRENCY_LEASE_TTL_SECONDS` (default 120). Config validation requires it to exceed
3 × (`DOGRAH_CONNECT_TIMEOUT_SECONDS` + `DOGRAH_READ_TIMEOUT_SECONDS`) — the longest sequence of
Dograh requests in one dial. Every key also carries a physical Redis TTL, so nothing is immortal.

The lease covers the **dial-processing window** (admission → outcome persisted), exactly what the old
counter covered. It is not released/extended based on call duration; Dograh status semantics are
unchanged. Because that window is bounded below the TTL, **no heartbeat is run**. `renew()` exists,
is tested, and is available if a later checkpoint extends the lease to call duration. If a dial ever
outlives its lease (pathological stall), its late release is a logged no-op and at worst one slot is
briefly over-admitted.

## 5. Crash recovery

A killed worker never releases; its lease expires after the TTL and the next acquire reclaims it
(`lease_stale_reclaimed` logged). No manual reset. Graceful shutdown already finishes the in-flight
job, so the `finally` release runs; the TTL is the final net, never the shutdown hook.
A job redelivered after a crash is rejected as `duplicate_lease` until the dead worker's lease expires
(≤ TTL), then admitted — it stays unacked meanwhile, never lost.

## 6. Failure policy

* Redis error at admission: the `RedisError` propagates (logged), nothing is dialed, the job stays
  unacked. No in-process fallback counter exists.
* Ambiguous acquire (reply lost): a best-effort release of that exact lease id is attempted.
* Redis error after acquire (CPS stage): the lease is released before the error propagates.
* Release failure: swallowed and logged; the TTL reclaims the slot.
* Redis data loss/restart: scripts reload automatically; forgotten leases mean temporarily
  *over*-admitting up to the configured limits, never a stuck-closed system.

## 7. Operations

* **Rollout:** old `concurrency:global|campaign:*|provider:*` counters are orphaned, and old/new workers
  do not share limits. Drain old workers, deploy, then `DEL` the old keys.
* Single Redis instance assumed (multi-key Lua scripts; Cluster would need hash tags).
* Inspect an attempt's lease: `GET concurrency:holder:<campaign>:<contact>:<attempt>`;
  active per scope: `ZCARD concurrency:lease:global`.
* Log events: `lease_acquired`, `lease_acquisition_rejected`, `lease_renewed`, `lease_released`,
  `lease_stale_reclaimed`, `lease_ownership_mismatch`, `lease_redis_operation_failed`
  (ids only — no phone numbers, secrets or payloads).
* **Metrics:** the repo has no metrics framework, so the requested counters
  (`concurrency_lease_acquired/rejected/released/expired_total`, `..._renewal_failure_total`,
  `..._ownership_mismatch_total`) are **not implemented**; the log events above map 1:1 to them.
* Known gap (pre-existing): the shared Redis client has no socket timeout.
