# Connection budget (P2-4)

## Formula

```
sum over services of ( replicas x processes x (DB_POOL_SIZE + DB_MAX_OVERFLOW) )
  + reserve  <=  Postgres max_connections
```

- **replicas**: containers of that service.
- **processes**: OS processes per container, each with its **own** pool. Today every service runs
  one. P2-6 adds `WEB_CONCURRENCY` for the API; a value of N makes the API row `1 x N x capacity`.
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

Sync API routes run in the AnyIO threadpool. `API_THREADPOOL_SIZE` defaults to the pool capacity of
the process (15), so threads and connections stay in proportion.

Measured [RAN], 1 vCPU sandbox, `WEB_CONCURRENCY=2`, concurrency 50, 10 s, mock provider, fast
queries (see `LOAD-SMOKE.md` once P2-8 lands):

| Threadpool | Read req/s | Read p95 | Write req/s | Write p95 | 5xx / pool timeouts |
|---|---|---|---|---|---|
| 15 (default) | 86 | 1605 ms | 69 | 1912 ms | 0 / 0 |
| 40 (old AnyIO default) | 79 | 1633 ms | 73 | 1822 ms | 0 / 0 |

**These numbers do not separate the two settings.** With fast queries the single CPU saturates
first and the difference is inside run-to-run noise, so the result neither contradicts nor proves
15. The default is therefore a *design* choice (threads proportional to connections, so overload
sheds as a fast 503 instead of queueing for a connection), not a measured optimum. P2-8 repeats the
sweep with a slow-query variant, where the two settings should differ, and this section is revised
there if the data disagrees.
