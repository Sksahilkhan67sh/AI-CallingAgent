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

Defaults are [ASSUMED] until P2-8 measures them; P2-4 and P2-8 may revise them with evidence.
