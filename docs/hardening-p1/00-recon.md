# P1-0 Recon — Phase 1 "Stop data loss"

Labels: **[RAN]** executed here, **[READ]** `file:line` on `main` @ `1e12d75`, **[ASSUMED]** not verified.
Integration branch: `main` — `develop` is a strict ancestor (0 commits ahead, 40 behind) so it does not contain `main` [RAN].

## 0. Baseline (Python 3.11.15, Postgres 16.15, Redis 7.0.15)

| Gate | Result |
|---|---|
| `pytest -q -p no:cacheprovider --ignore=tests/ops` | **1730 passed, 1 skipped**, 126 s [RAN] |
| `ruff check .` | clean [RAN] |
| `mypy app` | no issues in 182 files [RAN] |
| `alembic upgrade head && alembic check` | clean, single head `e2d1c0b9a8f7` [RAN] |

## 1. State machines

**Contact** (`models/enums.py:17-27`): Pending, Dialing, InConversation, Disconnected, RetryScheduled, Reconnecting, Completed, CompletedPartial, Closed.
**CallAttempt** (`models/enums.py:38-44`): Initiated, Connected, FailedToConnect, DroppedMidCall, EndedNormally.
There is **no** `needs_review` / `FAILED_UNKNOWN` value in either enum [READ] — P1-4 must add one or use another durable marker.

| Transition | Performed by | Where |
|---|---|---|
| Pending → Dialing, attempt created (Initiated), `attempt_count += 1` | dialer worker `_dial` | `dialer_worker.py:570-571` [READ] |
| claim committed **before** the HTTP trigger | `_place_call_via_dograh` | `dialer_worker.py:1123` (`db.commit()`) [READ] |
| trigger accepted → `provider_call_id` set, state stays Initiated | same | `dialer_worker.py:1236` [READ] |
| Initiated/Dialing → FailedToConnect / RetryScheduled / CompletedPartial | webhook service, `RecoveryManager`, `_record_dograh_trigger_failure` | `dograh_webhook_service.py`, `recovery/manager.py:170-239` [READ] |
| RetryScheduled → (dispatch) new DialJob on stream | `dispatch_due_recovery_jobs` | `recovery/dispatch.py:46-150` [READ] |

## 2. Redis keys

| Key | Purpose | TTL / bound |
|---|---|---|
| `calls:outbound` stream, group `dialer-workers` | dial jobs (`config.py:92-93`) | `XADD MAXLEN ~ 200_000` (`redis_queue.py:21,32,57`) |
| `recovery:scheduled` ZSET | retries + window-deferred first dials (`recovery/scheduler.py:24`) | none; **only copy of the due time** |
| `enqueued:<campaign>:<contact>:<n>` | duplicate-enqueue guard (`enqueue_service.py:44,51`) | 3600 s |
| `concurrency:lease:*`, `concurrency:holder:*` | CP12-A slot leases (`concurrency_lease.py:43-44`) | lease expiry |
| `circuit:<provider>:errors/open` | circuit breaker (`circuit_breaker.py:28,31`) | — |
| kill switch / provider halt | `outbound_control.KILL_SWITCH/PROVIDER_HALT` (`kill_switch.py:32`, `provider_halt.py:28`) | durable in PG since HF1 |
| `heartbeat:worker` | liveness (`system_service.py:19`) | `worker_heartbeat_ttl_seconds` |

## 3. Worker loop (`app/worker.py`)

- One job per iteration: `xreadgroup(..., count=1, block=1000)` (`redis_queue.py:83-85`) [READ].
- Reclaim runs **every 50 loop iterations** (`worker.py:85`), `XAUTOCLAIM` min-idle `queue_reclaim_idle_ms=30000`, **count=10** (`config.py:96`, `redis_queue.py:97`). It is iteration-based, not time-based [READ].
- Recovery dispatch every 10 iterations (`worker.py:36,116`) [READ].
- Every loop iteration the whole body is in `except Exception: rollback; log` (`worker.py:139-141`) — the loop never dies, but a failing message is simply re-read on the next reclaim, forever [READ].
- No per-job deadline; one hung Dograh call blocks the loop [READ].

## 4. `dialer_worker.py` flow (dequeue → ack)

`process_one_job` (:169) gate check → `read_one` (:191) → `process_claimed_job` (:365): kill switch/budget gate → pause check → window check → admission lease (≤5×0.2 s) → `_dial` → `db.commit()` → release lease → `queue.ack` unless outcome ∈ `_LEFT_UNACKED` (:199) [READ].
Ack points: 254, 257, 327, 330, 349, 462 [READ].

## 5. Recovery scheduler / manager / dispatch

- `RecoveryManager._schedule` does `scheduler.schedule()` (**Redis ZADD**) inside the open DB transaction, **before** commit (`manager.py:187`) [READ]. PG records only `RETRY_SCHEDULED`; the due time and next attempt number exist only in the ZSET member.
- `dispatch_due_recovery_jobs` does `scheduler.claim` (**ZREM**) first (`dispatch.py:55`); on any exception it rolls back and only logs (`dispatch.py:71-73`). The ZSET entry is already gone → the retry is lost and the contact stays `RETRY_SCHEDULED` [READ]. (Test in P1-2.)

## 6. Webhook path

`process_dograh_webhook` (`dograh_webhook_service.py:243`): idempotent on `ProcessedEvent(event_id="dograh:<run_id>")` (:251); refuses transitions out of a terminal attempt (:286-300) [READ]. Webhook is the only terminal signal for a Dograh call.

## 7. Failure matrix (today)

| Failure | Today |
|---|---|
| Worker killed before trigger, after claim commit | attempt `Initiated`, no `provider_call_id`. If the stream message survives, reclaim → `_resolve_unresolved_dograh_claim` (:578) → Dograh run lookup → else `RecoveryManager` retry [READ]. **If Redis is lost, nothing ever repairs it** (no sweeper) [READ] |
| Killed after trigger, before DB commit | same as above; Dograh may have placed the call → ambiguous [READ] |
| Killed after commit, before ack | reclaim → `_dial` sees `provider_call_id` → `ALREADY_PROCESSED` → acked (`dialer_worker.py:461-462`) [READ] |
| Redis restart/FLUSHALL | stream, group, guards, `recovery:scheduled` all gone; PENDING contacts re-enqueueable, `RETRY_SCHEDULED` contacts **stranded** [READ] |
| Redis stream > 200k entries, no consumer | **49,995 of 250,000 jobs dropped, contacts left PENDING with a live 1 h guard** [RAN] |
| Malformed stream payload | `DialJob.from_json` raises in `read_one` (:91) and in `reclaim_stale` (:117); message stays pending, `reclaim_stale` raises before any other reclaimed job is processed [READ] |
| Postgres down | exception → rollback, message unacked, redelivered after 30 s idle [READ] |
| Dograh timeout | classified AMBIGUOUS → run-listing reconciliation, then RecoveryManager backoff (can redial when lookup finds nothing) [READ] |
| Duplicate delivery | `existing.provider_call_id` set → `ALREADY_PROCESSED`; `UNIQUE(contact, attempt_number)` is the backstop [READ] |

Memory per stream entry: **≈511 bytes** (102,252,084 B for 200,005 entries) [RAN]; at the current cap ≈ 102 MB.

## 8. Verified bug list (evidence only)

| # | Bug | Evidence | Task |
|---|---|---|---|
| B1 | Stream trim drops undelivered jobs | reproduced, 49,995/250,000 lost [RAN] | P1-1 |
| B2 | Retries only in Redis; ZREM-before-process loses a retry on any exception | `dispatch.py:55,71`, `manager.py:187` [READ] → test in P1-2 | P1-2 |
| B3 | No repair for attempts stuck `Initiated` after Redis loss | no sweeper in `app/` for dial attempts [READ] | P1-4 |
| B4 | Poison payload blocks reclaim and loops forever | `redis_queue.py:91,117` [READ] → test in P1-5 | P1-5 |
| B5 | Reclaim is iteration-based; no per-job deadline | `worker.py:85`, no timeout around `process_one_job` [READ] | P1-6 |
| B6 | `get_redis()` has no socket timeouts | `core/redis_client.py:17` [READ] | P1-7 |
| B7 | Transcript fetch failure is indistinguishable from "empty" | task fact; confirm in P1-8 | P1-8 |

Known and deferred in earlier docs: H3 sweeper, H4 reclaim/DLQ, H5 reclaim timing, DB retry scheduling (`CHECKPOINT-12B-NOTES.md:117-118`); H2 enqueue pagination was fixed in CP12-C. The analysis stream has the same `MAXLEN ~ 200_000` trim (`analysis/queue.py:37`) but is repaired by the Postgres analysis sweeper (`analysis/sweeper.py`) — noted in `backlog.md`, not changed.
Anything not above has no evidence yet and goes to `backlog.md`.
