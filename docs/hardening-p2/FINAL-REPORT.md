# Phase 2 final report: database resilience and performance

Base `71b1511` (Phase 1 complete). Labels: **[RAN]** executed, **[READ]** read from a file,
**[ASSUMED]**. All local numbers: 1 vCPU, 4 GB sandbox, Python 3.11, PostgreSQL 16, Redis 7.

## Tasks and pull requests
| ID | Task | PR | Result |
|---|---|---|---|
| P2-0 | Recon | #88 | Baseline of today's failure modes, measured [RAN] |
| P2-1 | Dockerfile pins, Dependabot ignores, `age` in CI, ops skip budget | #89 | CI skips in `tests/ops` 43 of 111 -> 0 of 111 |
| P2-2 | Pool: pre-ping, overflow, timeout, recycle; 503 + `Retry-After` | #93 | Killed pool -> 0 5xx (was 1 per dead connection) |
| P2-3 | Statement, lock and idle-in-transaction timeouts per role | #94 | All large operations complete under the defaults |
| P2-4 | Threadpool vs pool, connection budget | #95 | Budget document checked against compose; **default later corrected in P2-8** |
| P2-5 | One-shot `migrate` job, advisory lock, retry on blocked migrations | #96 | Two concurrent migrators: 10/10 pass (was 3/3 failure) |
| P2-6 | `uvicorn --workers`, graceful shutdown | #98 | Two PIDs; SIGTERM mid-request completes in-flight work |
| P2-7 | Database-resilience gate and mutation checks | #99 | Gate green; 4/4 mutations make it fail |
| P2-8 | Load smoke and capacity numbers | #100 | Old code collapses at 200 clients, new code does not |
| P2-9 | Close-out | #102 | This report |
| (incident) | certifi 2026.10.19 for PYSEC-2026-4320 | #97 | Unblocked the Dependency audit for every PR |

## Test counts (Python 3.11, local) [RAN]
Before (main `71b1511`): **1975 passed, 13 skipped**. After: **2080 passed, 13 skipped** (+105
tests). CI skip count in `tests/ops`: **43 of 111 before, 0 of 111 after** (P2-1), enforced by an
allowance of 0. Every merge had three green checks (Backend, Frontend, Dependency audit) on its
exact head SHA, and main's CI was green after each merge.

## Gate and mutation checks (P2-7) [RAN]
Gate `tests/test_p2_db_resilience.py`: 9 passed, 1 skipped (the optional real-restart scenario,
which I ran by hand against a real `pg_ctlcluster restart` and passed). It kills the
application's own connections inside every provider call and checks no duplicate dial and no lost
job, plus the API, sweeper and worker afterwards, pool exhaustion, the three timeouts, two
concurrent migrators, and that the Phase 1 gate is byte-identical and passes.
Mutations, each **makes the gate FAIL** (baseline passed, tree restored): disable `pool_pre_ping`;
remove the statement timeout; remove the advisory lock; remove the 503 mapping.
Under `faketime` at three timestamps: 9 passed, 1 skipped each. Repeats: light scenarios x200 =
600/600, heavy x10 = 60/60. **Not run: the whole file x200** (about 90 minutes).

## Load numbers (P2-8) [RAN, single host, mock provider, not a capacity claim]
"Before" = the pre-Phase-2 commit through the same script. 2 web processes, 30 connections.
| Scenario (conc. 200 unless noted) | Before | After |
|---|---|---|
| Fast read / write (10, 50, 100) | 0 5xx | 0 5xx, same latency (CPU-bound) |
| Holds a connection 500 ms | collapse: 11 req/s, 97 x HTTP 500, max 63 s | 66 req/s, 0 errors, p95 3.8 s |
| Holds 2000 ms (conc. 400) | 233 x 500 and 109 client timeouts at 120 s, p50 91 s | 453 x 503, no 500, no timeout, max 36 s |
Knee: none up to 200 clients with 500 ms holds; between 100 and 200 with 2 s holds.
**P2-4's threadpool default (1x the pool capacity) was wrong**: 15 or 30 threads per process
collapse at 100 clients, 60 holds at 100, 120 holds at 200. The default is now 8x capacity (120).

## Settings added (default; production advice). Full detail: `SETTINGS.md`
| Setting | Default | Production advice |
|---|---|---|
| `DB_POOL_SIZE` | 5 | per process; keep the fleet sum under `max_connections` |
| `DB_MAX_OVERFLOW` | 10 | capacity per process = size + overflow |
| `DB_POOL_TIMEOUT` | 10 s | short: bounds the wait before a 503 |
| `DB_POOL_RECYCLE` | 1800 s | below any proxy idle limit |
| `DB_POOL_PRE_PING` | true | leave on |
| `DB_RETRY_AFTER_SECONDS` | 2 | `Retry-After` on a DB 503 |
| `DB_ROLE` | api | worker, analysis, sweeper, script for those processes (compose sets it) |
| `DB_STATEMENT_TIMEOUT_MS` | 15000 | API; measured slowest statement 81 ms |
| `DB_LOCK_TIMEOUT_MS` | 5000 | API |
| `DB_IDLE_IN_TX_TIMEOUT_MS` | 30000 | API |
| `DB_WORKER_STATEMENT_TIMEOUT_MS` | 60000 | background roles |
| `DB_WORKER_LOCK_TIMEOUT_MS` | 15000 | background roles |
| `DB_WORKER_IDLE_IN_TX_TIMEOUT_MS` | 120000 | must exceed `JOB_DEADLINE_SECONDS` (boot refuses otherwise) |
| `DB_MIGRATION_LOCK_TIMEOUT_MS` | 10000 | alembic only (no statement timeout) |
| `RUN_MIGRATIONS` | 0 | 1 only for a single container with no migrate job |
| `DB_MIGRATION_LOCK_WAIT_SECONDS` | 300 | wait for another migrator |
| `DB_MIGRATION_LOCK_RETRIES` | 3 | attempts when a long transaction blocks a migration |
| `DB_MIGRATION_RETRY_DELAY_SECONDS` | 2 | between attempts |
| `API_THREADPOOL_SIZE` | unset = 8 x capacity (120) | do not go below 2 x capacity (startup warning) |
| `DB_MAX_CONNECTIONS_BUDGET` | unset | per-process cap; boot fails if the pool is larger |
| `WEB_CONCURRENCY` | 1 | multiplies the API's connections: recompute `CONNECTION-BUDGET.md` first |
| `WEB_GRACEFUL_SHUTDOWN_SECONDS` | 30 | below the container's `stop_grace_period` (40 s) |
| `WEB_KEEP_ALIVE_SECONDS` | 5 | below any load balancer idle timeout |
Connections now also carry `application_name = aca-<role>`. A test fails if a `db_*`, `web_*` or
`api_threadpool*` setting is not in both `.env.example` and `SETTINGS.md`.

## HUMAN-TODO (not verifiable in the sandbox: no Docker)
1. `docker build ./backend && docker build ./frontend` (never built).
2. `docker compose config && docker compose up -d`; confirm `migrate` exits 0 before `backend`,
   `worker`, `analysis-worker` and `sweeper` start.
3. `docker compose stop backend` with a slow request in flight: it should complete and the
   container exit 0 within the 40 s grace period.
4. Run the gate with `P2_PG_RESTART_CMD` set to your stack's restart command; I only restarted
   the sandbox's server.
5. Rotate the GitHub token used for this work (it appeared in the working transcript).

## Remaining risks
* Compose and Docker behaviour above is unverified; the YAML is parsed and asserted by tests only.
* The threadpool default (120) rests on a single-CPU sandbox sweep (15/30/60/120 threads tested;
  240 untested). The stall mechanism is a hypothesis the sweep supports, not traced.
* Pool, timeout and threadpool defaults are justified on this sandbox, not on production data.
* The worker and analysis worker may spin in a tight error loop during a long database outage:
  not measured.
* `ubuntu-latest` is moving to Ubuntu 26: the `apt install age shellcheck` step and the Postgres
  paths used by the ops drills may break.
* libfaketime stalls timed waits: two P2-2 tests run with the monotonic clock real, and the
  gate's server and nested Phase 1 run use a real clock. The Phase 1 gate file itself hangs under
  the default faketime (not changed).
* Whole-file x200 repeat of the gate was not run. One local Phase 1 failure and one skip-count
  change were not explained (the failure vanished after a sandbox reset).
* `reconcile_suppressions` and per-subject erasure were not timed at volume.
* Python Dependabot also ignores minor bumps (beyond the brief), because 3.11 -> 3.14 is "minor".
* Dependabot PR #101 (python minor/patch bumps) was open when this phase closed and was not
  touched: it must pass the same three checks, and the constraints check, before anyone merges it.
* Two PRs (#89, #97) were merged by the account that owns the token before I merged them; both
  had green checks on the head SHA. Merge authority should be agreed.

Everything found but not fixed is in `backlog.md`.

Phase 2 is complete only when P2-7 passes in CI on main and main is green after the last merge.
