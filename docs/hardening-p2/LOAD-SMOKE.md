# Load smoke and capacity numbers (P2-8)

**This is a single-host, mock-provider smoke test. It is not a production capacity claim.**
Machine: 1 vCPU (Intel Xeon @ 2.10 GHz), 4 GB RAM, Ubuntu 24.04, Python 3.11, PostgreSQL 16 and
Redis 7 on the same host, the load generator (httpx/asyncio) on the same host and CPU, a database
holding about 300,000 contacts. No real provider: nothing dials. Tool: `ops/loadtest/`
(`loadtest.py`, `slow_app.py`, `run_matrix.sh`, README).

**Before** = the pre-Phase-2 commit `71b1511` run through the same script (checked out in a
worktree), so it is genuinely the old behaviour, including the old raw 500 on a pool timeout.
**After** = `main` with the new defaults. API: 2 web processes (`WEB_CONCURRENCY=2`); each process
has a pool of 5 + 10 connections, so 30 connections in total. Mutation rate limiter lifted
(otherwise the write test measures HTTP 429).

## 1. Fast queries (CPU-bound): no difference, no errors
10 s per point, `GET /api/v1/campaigns?limit=50` and `POST /api/v1/campaigns`.

| Endpoint | Conc. | Before req/s, p95 | After req/s, p95 | 5xx (both) |
|---|---|---|---|---|
| read | 10 | 123, 190 ms | 94, 224 ms | 0 |
| read | 50 | 94, 1538 ms | 78, 1966 ms | 0 |
| read | 100 | 77, 3799 ms | 72, 4450 ms | 0 |
| write | 10 | 102, 163 ms | 105, 165 ms | 0 |
| write | 50 | 79, 1747 ms | 80, 1730 ms | 0 |
| write | 100 | 66, 4259 ms | 64, 4427 ms | 0 (1 client `ReadError` in one "after" run) |

The single CPU saturates first, so latency grows and nothing errors, before and after. The
differences between the two columns are within run-to-run noise (the same code varied by about 20%
between runs). Fast queries cannot show pool behaviour; that needs section 2.

## 2. A request that holds a connection (DB-bound): where Phase 2 matters
`GET /_db_slow?ms=N` holds a connection for N ms with a server-side sleep (no CPU).
Capacity here is 30 connections / hold time: 60 req/s at 500 ms, 15 req/s at 2000 ms.

**500 ms hold, 20 s per point**

| Conc. | Before | After |
|---|---|---|
| 10 | 20 req/s, p95 517 ms, 0 errors | 20 req/s, p95 518 ms, 0 errors |
| 50 | 60 req/s, p95 1018 ms, 0 errors | 60 req/s, p95 1016 ms, 0 errors |
| 100 | 58 req/s, p95 3621 ms, 0 errors | 62 req/s, p95 1865 ms, 0 errors |
| 200 | **collapse**: 11 req/s, 97 x HTTP **500**, p50 31 s, **max 63 s** | 66 req/s, p95 3811 ms, **0 errors** |

**2000 ms hold, 30 s per point (the harsher stage: queue wait can exceed the pool timeout)**

| Conc. | Before | After |
|---|---|---|
| 100 | 10 req/s; 21 x **500**; max 34.8 s | 17 req/s; 4 x 503; max 10.3 s |
| 200 | 7 req/s; 93 x **500**; max **68.6 s** | 23 req/s; 103 x 503; max 12.7 s |
| 400 | 14 req/s; 233 x **500** and 109 client **timeouts at 120 s**; p50 91 s | 30 req/s; 453 x 503; 447 x 200; max 36.2 s |

**The knee.** With 500 ms holds there is no knee up to 200 concurrent clients after the change
(the queue wait, about (conc - 30) / 60 s, stays far below `DB_POOL_TIMEOUT`); before, it was
between 100 and 200. With 2000 ms holds errors start between 100 and 200 clients, where the queue
wait for a connection exceeds the 10 s pool timeout. The two web processes did not receive equal
shares of the connections, which is why a few 503s already appear at 100.

**What "graceful" means here.** Before: raw HTTP 500, latencies up to 69 s, and client timeouts at
120 s, i.e. hangs. After: no 500 and no client timeout in any run; overload is a 503 with
`Retry-After` after at most about the pool timeout plus the queue ahead of it (36 s at 400
clients with 2 s holds, which is queueing for a thread, not a hang).

## 3. The threadpool default was wrong in P2-4; this is the evidence that fixed it
P2-4 set `API_THREADPOOL_SIZE` equal to the pool capacity (15) on the belief that overload would
then shed as a fast 503. The measurement says otherwise. 500 ms hold, 20 s, 2 web processes:

| Threads per process | Conc. 100 | Conc. 200 |
|---|---|---|
| 15 (the P2-4 default) | **collapse**: 8 req/s, 32 x 503, p50 12 s | **collapse**: 11 req/s, 144 x 503, max 54 s |
| 30 | partial: 35 req/s, 37 x 503, p95 11 s | 14 req/s, 116 x 503 |
| 60 | healthy: 62 req/s, 0 errors | degraded: 29 req/s, 124 x 503 |
| 120 | healthy: 62 req/s, 0 errors | healthy: 67 req/s, 0 errors, p95 3.7 s |

*Mechanism (a hypothesis the sweep supports, not something I traced in the runtime).* A request's
connection is released by a separate threadpool call (the session clean-up of the `get_db`
dependency). With few threads, new requests take every thread and block waiting for a connection
that a finished request cannot release until a thread frees up; the stall lasts until the pool
timeout fires. More threads than connections, by a wide margin, avoids it.

**Decision:** the unset default is now `8 x (DB_POOL_SIZE + DB_MAX_OVERFLOW)` = 120 per process,
the smallest tested value that held at 200 clients. 240 and other values were not tested. A startup
warning fires when the threadpool is below 2x the pool capacity (30), the region where the stall
was measured. This replaces P2-4's warning, which pointed the other way.

## 4. Caveats
Single host and a single CPU shared with the load generator; a mock provider (no dialing); the
slow endpoint holds a connection with a sleep, which costs no CPU, so a real slow query that also
burns CPU will behave differently; one run per point (no confidence intervals), so differences
under about 20% are noise; two web processes split connections unevenly. Numbers will differ on
other hardware. Do not quote them as capacity.
