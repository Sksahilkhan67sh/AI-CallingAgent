# Connection budget (P2-4)

## Formula

```
sum over services of ( replicas x processes x (DB_POOL_SIZE + DB_MAX_OVERFLOW) )
  + reserve  <=  Postgres max_connections
```

- **replicas**: containers of that service.
- **processes**: OS processes per container, each with its **own** pool. Today every service runs
  one. The API's process count is `WEB_CONCURRENCY` (P2-6, default 1): a value of N makes the
  `backend` row `1 x N x capacity`, so raise it only after recomputing the sum below.
- **reserve**: connections that are not a steady pool (see the Reserve table).
- Pool capacity per process is `DB_POOL_SIZE + DB_MAX_OVERFLOW` (5 + 10 = 15 by default). A pool
  only opens overflow connections under load, so this is the worst case, which is what a budget
  must cover.

Set `DB_MAX_CONNECTIONS_BUDGET` on each service to its share; the app refuses to start if its
pool is larger.

## Compose defaults

Rows are the compose services that connect to the database (`PRIMARY_DB_URL`).

| Service | Replicas | Processes | Pool | Overflow | Worst case |
|---|---|---|---|---|---|
| backend | 1 | 1 | 5 | 10 | 15 |
| worker | 1 | 1 | 5 | 10 | 15 |
| analysis-worker | 1 | 1 | 5 | 10 | 15 |
| sweeper | 1 | 1 | 5 | 10 | 15 |

Steady total: 60 connections.

## Reserve

| Item | Connections |
|---|---|
| `superuser_reserved_connections` (Postgres default, for emergency access) | 3 |
| Ops CLIs run by hand (retention, erasure, restore report): one process each, pool capacity capped by their own env | 10 |
| One-shot migrator (P2-5): `NullPool`, one connection | 1 |

Reserve: 14. Worst case total: 60 + 14 = 74.

`docker-compose.yml` sets `max_connections=100` explicitly, so the compose stack has 26
connections of slack. A test (`tests/test_p2_4_connection_budget.py`) fails if the table, the
compose services or `max_connections` drift apart.

## Scaling checklist

1. Adding replicas or processes multiplies a row: recompute the sum before deploying.
2. Prefer lowering `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` per process over raising `max_connections`:
   each Postgres connection costs memory and the scheduler degrades well before 100s of sessions.
3. Beyond a few dozen processes put a pooler (PgBouncer) in front. Out of scope here.

## Threadpool

Sync API routes run in the AnyIO threadpool, per web process. `API_THREADPOOL_SIZE` defaults to
**8 x the pool capacity** of the process (120 for 5 + 10).

This replaces the P2-4 default of 1 x capacity, which was wrong: P2-8 measured that with 15 or 30
threads per process the API stalls under load (a connection can be held by a finished request
that is waiting for a free thread to release it), while 120 held at 200 concurrent clients with
no errors. The sweep and the mechanism are in `LOAD-SMOKE.md` section 3. The earlier P2-4 sweep
(fast queries, 15 vs 40) could not separate these settings because the single CPU saturates first.

A startup warning fires when the threadpool is below 2 x the pool capacity. Threads are cheap
relative to connections (they are not connections): raising the thread count does not raise the
connection budget above, which stays `WEB_CONCURRENCY x (DB_POOL_SIZE + DB_MAX_OVERFLOW)`.
