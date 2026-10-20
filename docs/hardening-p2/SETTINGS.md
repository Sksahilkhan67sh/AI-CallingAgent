# Phase 2 settings added

Every value is **per process**. The API, worker, analysis worker and sweeper each build their own
engine, so the database must have room for the sum (see `CONNECTION-BUDGET.md`, P2-4). Override per
service through that service's environment.

| Setting (env) | Default | Task | Production recommendation |
|---|---|---|---|
| `DB_POOL_SIZE` (legacy name `PRIMARY_DB_POOL_SIZE` still read) | 5 | P2-2 | Persistent connections per process. Start at 5; raise only if requests queue for a connection (503s with `QueuePool` in the log). Re-check the connection budget when you change it. |
| `DB_MAX_OVERFLOW` | 10 | P2-2 | Extra short-lived connections above the pool size. Pool capacity per process is `DB_POOL_SIZE + DB_MAX_OVERFLOW`; keep the sum over all processes below Postgres `max_connections`. |
| `DB_POOL_TIMEOUT` | 10 (seconds) | P2-2 | How long a request waits for a free connection before it gets a 503 + `Retry-After`. Keep it short: a request waiting longer than this is already too slow for a user. |
| `DB_POOL_RECYCLE` | 1800 (seconds) | P2-2 | Replace a connection after this long. Set below any idle-connection limit of a proxy or load balancer in front of Postgres. 0 disables. |
| `DB_POOL_PRE_PING` | true | P2-2 | Leave on. Costs one trivial round trip per checkout and replaces connections the server already dropped (restart, failover) instead of failing a request. |
| `DB_RETRY_AFTER_SECONDS` | 2 | P2-2 | Value of `Retry-After` on a 503 caused by an unavailable database. |
| `DB_ROLE` | api | P2-3 | `api`, `worker`, `analysis`, `sweeper` or `script`. Selects which set of limits a process gets; an unset role gets the strict API limits. Compose sets it for worker, analysis-worker and sweeper; run ops CLIs with `DB_ROLE=script`. |
| `DB_STATEMENT_TIMEOUT_MS` | 15000 | P2-3 | API: server cancels any statement longer than this (-> 503). Measured slowest API statement at 210K contacts: 81 ms. 0 disables. |
| `DB_LOCK_TIMEOUT_MS` | 5000 | P2-3 | API: how long a statement waits for a lock held by another session before failing (-> 503 + `Retry-After`). |
| `DB_IDLE_IN_TX_TIMEOUT_MS` | 30000 | P2-3 | API: the server terminates a session whose transaction sits idle this long (a leaked transaction). |
| `DB_WORKER_STATEMENT_TIMEOUT_MS` | 60000 | P2-3 | Same, for worker / analysis / sweeper / script roles. |
| `DB_WORKER_LOCK_TIMEOUT_MS` | 15000 | P2-3 | Same, for those roles. |
| `DB_WORKER_IDLE_IN_TX_TIMEOUT_MS` | 120000 | P2-3 | Same, for those roles. **Must exceed `JOB_DEADLINE_SECONDS` x 1000** (the worker holds its transaction open during the provider call); the app refuses to boot otherwise. |
| `DB_MIGRATION_LOCK_TIMEOUT_MS` | 10000 | P2-3 | Alembic only: bounded wait for locks. Alembic always runs with no statement and no idle limit, whatever the migration role's own defaults are. |
| `API_THREADPOOL_SIZE` | unset = `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15) | P2-4 | Threads that run sync API routes (the AnyIO default is 40). Equal to the pool capacity, request threads cannot outnumber connections; larger only logs a startup warning (valid if many requests never touch the database). Per web process (see P2-6). |
| `DB_MAX_CONNECTIONS_BUDGET` | unset | P2-4 | Most connections this process may hold. Set per service from `CONNECTION-BUDGET.md`; the app **fails to boot** if `DB_POOL_SIZE + DB_MAX_OVERFLOW` exceeds it. |

Defaults are [ASSUMED] until P2-8 measures them; P2-4 and P2-8 may revise them with evidence.
