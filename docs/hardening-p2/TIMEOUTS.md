# Statement, lock and idle-in-transaction timeouts (P2-3)

Limits are applied by the **server** on every new connection through libpq `options`
(`-c statement_timeout=... -c lock_timeout=... -c idle_in_transaction_session_timeout=...`), chosen
by `DB_ROLE`. Defaults are in `SETTINGS.md`.

| Role | Statement | Lock | Idle in transaction |
|---|---|---|---|
| `api` (default) | 15 s | 5 s | 30 s |
| `worker`, `analysis`, `sweeper`, `script` | 60 s | 15 s | 120 s (must exceed the job deadline) |
| alembic | none | 10 s | none |

## What each limit does to a client [RAN]

| Event | What the database does | What the API answers | Workers |
|---|---|---|---|
| Statement over the limit | cancels it (`QueryCanceled`, an `OperationalError`) | 503 + `Retry-After` | the existing catch / rollback / no-ack / reclaim path |
| Row lock held by another session | `LockNotAvailable` (an `OperationalError`) after `lock_timeout` | 503 + `Retry-After` | same |
| Transaction left idle | terminates the session; the next statement raises **`InternalError`** (SQLSTATE 25P03), not `OperationalError` | 503 + `Retry-After` (a dedicated, narrow mapping; any other `InternalError` stays 500) | same |

Found while testing: the third row is not an `OperationalError`, so P2-2's handler alone would have
answered 500 for it.

## Measured long operations under the DEFAULT limits [RAN]

Machine: 1 vCPU / 4 GB sandbox, PostgreSQL 16, database holding about 310,000 contacts. "Slowest
statement" is the maximum single SQL statement (SQLAlchemy cursor hook). Every operation completed
with nothing cancelled.

| Operation | Role that runs it | Volume | Wall | Slowest statement | Headroom vs limit |
|---|---|---|---|---|---|
| Campaign enqueue walk (500-row pages), run inside the API | api (15 s) | 100,000 contacts, 404 statements | 22.9 s | 973 ms (the bench's own seed `INSERT`; enqueue statements are lower) | >15x |
| CSV import (row cap 10,000; 140 KB, the 5 MB byte cap is not reached) | api (15 s) | 10,000 rows | 2.5 s | 98 ms | >150x |
| Retention purge (`DELETE ... id IN (500)`) | script (60 s) | 200,000 aged rows, 808 statements | 7 s | 75 ms | >800x |
| Post-restore report (count / max over every table) | script (60 s) | 310K contacts | 2 s | 610 ms | >98x |
| Admin dashboard and list reads | api (15 s) | 210K contacts | 0.01-0.15 s each | 81 ms | >180x |

Not measured at volume: `reconcile_suppressions` (needs input files; reads the suppression list
once, which was empty here) and per-subject erasure (bounded by one person's rows). Both run as
`script` (60 s). `pg_dump` / `psql` based backup and verify tooling connects directly, not through
the application engine, so these limits do not apply to it. [ASSUMED]

Every long operation found is either batched far below its limit or runs in a role whose limit it
clears by a wide margin, so no `SET LOCAL statement_timeout` override was needed and none was added.
If one is ever needed it must be a named, setting-driven value, not a literal.

## Alembic

Migrations run with no statement timeout and a bounded `lock_timeout`, overriding whatever the
migration role has as its own default. Test: a role with `statement_timeout = 1ms` still runs
`alembic upgrade head` to completion (it failed with `QueryCanceled` before this change [RAN]).
Retry on a lock timeout and serialisation of concurrent migrators are P2-5.
