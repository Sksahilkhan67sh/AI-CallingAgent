# Phase 2 "Database resilience and performance" state

Integration branch: `main`. Labels: [RAN] / [READ] / [ASSUMED].

| ID | Task | Status | PR# | Evidence | Iteration |
|---|---|---|---|---|---|
| P2-0 | Recon | MERGED | #88 | `00-recon.md`; baseline 1975 passed, 13 skipped [RAN]; 5xx per dead connection, 45 s lock hang, pool timeout = 500 after 30 s, 3/3 concurrent-alembic crash all reproduced [RAN] | 1 |
| P2-1 | Carry-over hygiene (Dockerfile pins, age in CI) | MERGED | #89 | CI skip count in tests/ops 43 of 111 -> 0 of 111 (annotations on 41c1496 / 1ef54a7) [RAN]; 9 new tests failed before, pass after incl. faketime x3 [RAN]; Dockerfiles pinned python:3.11-slim / node:22-slim; Dependabot ignores added; `docker build` of both is HUMAN-TODO (no Docker): `docker build ./backend && docker build ./frontend` | 2 |
| P2-2 | Pool: dead connections, fail fast | MERGED | #93 | kill all pooled conns -> 50 requests 0 5xx (was [500,200,...]) [RAN]; pool_size=2/overflow=0/timeout=1 with 10 slow requests -> 8 x 503 in ~1 s, 2 ok [RAN]; real WorkerLoop survives the kill and dials once, 500/500 repeats [RAN]; 16 tests red before (15 failed) -> 17 pass; full suite 2000 passed, 13 skipped [RAN]; faketime x3 pass (see PR for the monotonic-clock caveat) | 3 |
| P2-3 | Statement/lock/idle timeouts | MERGED | #94 | statement over limit cancelled+503, row lock fails within lock_timeout, leaked tx terminated by server, alembic works under a role with statement_timeout=1ms (failed before) [RAN]; 100K enqueue (api limits) 22.9 s, CSV 10K, retention 200K, restore report all complete under defaults, slowest statement 973 ms [RAN]; found idle-tx kill surfaces as InternalError (mapped to 503) [RAN]; 20/20 red before -> 24 pass; full suite 2023 passed, 13 skipped; faketime x3 pass under the exact rule command [RAN] | 4 |
| P2-4 | Pool vs threadpool, connection budget | MERGED | #95 | API_THREADPOOL_SIZE (default = pool capacity) applied via lifespan; DB_MAX_CONNECTIONS_BUDGET fails boot; max_connections=100 explicit in compose; CONNECTION-BUDGET.md table (60 + reserve 14 = 74) checked against real compose by test [READ]; 12 tests pass, faketime x3 pass; full suite 2035 passed, 13 skipped [RAN]; threadpool 15 vs 40 sweep inconclusive on fast queries (revisit in P2-8) [RAN] | 5 |
| P2-5 | Migrations as one-shot job | MERGED | #96 | two concurrent alembic upgrades on a fresh DB: both exit 0, one head, no duplicate DDL, 10/10 repeats (failed 3/3 before) [RAN]; blocked by held lock -> aborts cleanly after retries at previous revision, succeeds when blocker releases [RAN]; bounded wait on advisory lock [RAN]; entrypoint migrates only with RUN_MIGRATIONS=1; compose migrate service + service_completed_successfully wiring parsed [READ]; 13 of 15 tests red before; full suite 2050 passed, 13 skipped; faketime x3 pass (exact command) [RAN]; docker compose config NOT run (HUMAN-TODO) | 6 |
| P2-6 | Web concurrency, graceful shutdown | MERGED | #98 | decision: uvicorn --workers (no new dependency; see SHUTDOWN.md); WEB_CONCURRENCY=2 serves from 2 PIDs, 25/25 repeats [RAN]; SIGTERM with 4 in-flight requests: all 200, exit 0, new connections refused [RAN]; a 30 s request with 2 s grace does not delay exit [RAN]; worker/analysis/sweeper exit cleanly on SIGTERM, no loss or hang found so no change [RAN]; 11 of 11 first tests red; full suite 2065 passed, 12 skipped; faketime x3 pass (exact command) [RAN]; Docker stop behaviour NOT run (HUMAN-TODO) | 7 |
| P2-7 | DB-resilience gate | TODO | | | |
| P2-8 | Load smoke, capacity numbers | TODO | | | |
| P2-9 | Close-out | TODO | | | |

## Resume point
Next: start P2-7 (branch `fix/p2-7-resilience-gate`).

## HUMAN-TODO
- P2-1: `docker build ./backend` and `docker build ./frontend` (never built in this sandbox).
- P2-5: `docker compose config && docker compose up -d`; confirm `migrate` exits 0 before backend/worker/analysis-worker/sweeper start.
- P2-6: `docker compose up -d && docker compose stop backend` with a slow request in flight; confirm it completes and the container exits 0 within the 40 s grace period.

## Incidents
- 2026-10-21, PR #97: the Dependency audit check started failing for every commit (including PR #96 for P2-5, which changes no dependency) because PYSEC-2026-4320 was published against `certifi==2026.7.22` (bundled CA store trusted a root whose intermediates have compromised keys; fixed in 2026.10.19). Fixed by pinning only `certifi==2026.10.19` in `backend/constraints.txt` [RAN: `scripts/constraints.sh check`, `audit_gate.py pip` and `audit_gate.py npm` all clean locally]. Not a Phase 2 task; no other dependency changed.
