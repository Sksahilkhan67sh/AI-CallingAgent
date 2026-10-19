# Phase 2 recon: database resilience and performance

Base: `main` @ `71b1511`. Labels: **[RAN]** executed here, **[READ]** `file:line`, **[ASSUMED]**.
Machine: 1 vCPU, 4 GB RAM, Ubuntu 24.04, Python 3.11.15, PostgreSQL 16.13 and Redis 7.0 installed
locally (no Docker). All timings are single-host sandbox numbers, not production figures.

## 1. Engine and session creation

| Fact | Evidence |
|---|---|
| One engine per process: `create_engine(url, pool_size=...)` and nothing else. Defaults therefore apply: `max_overflow=10`, `pool_timeout=30 s`, `pool_pre_ping=False`, `pool_recycle=-1`, no `connect_args`, no server-side timeouts. | [READ] `backend/app/core/database.py:23` |
| `primary_db_pool_size` (5) is the only pool setting. | [READ] `backend/app/core/config.py:97` |
| `get_db` yields a session, commits after the endpoint returns, rolls back on error, always closes. `DB_SESSION = Depends(get_db, scope="function")` shares one session per request. | [READ] `database.py:28-47` |
| No `statement_timeout`, `lock_timeout` or `idle_in_transaction_session_timeout` anywhere in the app. | [READ] grep of `backend/app` |
| No AnyIO limiter is configured, so sync routes run in the default threadpool of **40** tokens, against a pool capacity of 5 + 10 = **15**. | [READ] grep `anyio\|to_thread\|total_tokens` in `backend/app` is empty |
| No handler maps `OperationalError`, `DisconnectionError` or the pool `TimeoutError`; only `IntegrityError` has one. | [READ] `backend/app/main.py:142-150` |

## 2. Process entrypoints

| Process | How it opens sessions | Evidence |
|---|---|---|
| API | `get_db` via `DB_SESSION` | [READ] `database.py:47` |
| worker | `SessionLocal` passed as a factory | [READ] `app/worker.py:22,55,94` |
| analysis worker | `SessionLocal()` per job | [READ] `app/analysis_worker.py:79` |
| sweeper | `sweep_stuck_attempts(SessionLocal)` | [READ] `app/sweeper.py:32` |
| ops scripts | `with SessionLocal() as db` (retention, erase, reapply erasures, reconcile, export/dedupe suppressions, backfill, renormalize, post-restore report) | [READ] `app/scripts/*.py` |
| alembic | its own engine, `NullPool`, no advisory lock, no timeouts | [READ] `backend/alembic/env.py:42` |

All four long-running processes share the same engine module, so they get identical pool settings
today: there is no per-role configuration.

## 3. Containers

| Fact | Evidence |
|---|---|
| `docker-entrypoint.sh` runs `alembic upgrade head` before `exec "$@"`, and it is the ENTRYPOINT of the shared image, so **backend, worker, analysis-worker and sweeper each migrate on start**. | [READ] `backend/docker-entrypoint.sh:7`, `backend/Dockerfile:30`, `docker-compose.yml:56,71` |
| API runs `uvicorn app.main:app` with no `--workers`, no graceful-shutdown or keep-alive flags (one process). | [READ] `backend/Dockerfile:31` |
| Postgres is `postgres:16` with no `max_connections` override, so the default **100** applies (confirmed 100 on the local 16.13 server [RAN]). | [READ] `docker-compose.yml:3` |
| Compose defaults: 4 app processes x (5 + 10) = **60** possible connections, plus alembic and tooling. | [READ] derived from the above |
| `backend/Dockerfile` is `python:3.14-slim` and `frontend/Dockerfile` is `node:25-slim`; CI runs Python 3.11 and Node 22. | [READ] `backend/Dockerfile:1`, `frontend/Dockerfile:1,8`, `.github/workflows/ci.yml:94,124` |

## 4. Behaviour today [RAN]

Real `uvicorn` against local Postgres, default settings (`/home/claude/recon/probe.py`, copied into
`ops/loadtest/` in P2-8).

| Scenario | Result |
|---|---|
| **All pooled connections killed** with `pg_terminate_backend`, then 50 sequential requests | **1 x HTTP 500** (`OperationalError: terminating connection due to administrator command`) **plus 1 client `ReadError`**, then 48 x 200. The pool self-heals but every dead connection costs a real 5xx (with N concurrent connections, up to N). |
| **Postgres restart** under a live server, 30 sequential requests | Same: 1 x 500 + 1 `ReadError`, then 28 x 200. No app restart needed. |
| **Lock held by another session** (ACCESS EXCLUSIVE on `campaign` for 45 s), 20 concurrent GETs | **15 requests hung for the full 45 s** (nothing bounds a lock wait) and returned 200 only after the lock was released. |
| **Pool exhausted** (same run: pool 5 + overflow 10 = 15 busy) | The other **5 requests waited exactly ~30 s** then failed as **HTTP 500** with `sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached`. A raw 500, not a 503, and no `Retry-After`. |
| **Two concurrent `alembic upgrade head`** on a fresh DB, 3 runs | **3/3: one exits 1** with `UniqueViolation ... (typname, typnamespace)=(alembic_version, 2200) already exists`. The other exits 0. |

## 5. Long statements, measured max single-statement duration [RAN]

Timed with a SQLAlchemy `before/after_cursor_execute` hook (`/home/claude/recon/maxstmt.py`).

| Operation | Volume | Wall time | Slowest single statement |
|---|---|---|---|
| Enqueue walk (`QueueEnqueueService`, 500-row pages) | 100,000 contacts, 404 statements | 24-26 s | below 721 ms (721 ms is the bench's own seed `INSERT`, which is included in the timer) |
| Retention purge (`run_retention --apply`, batch 500) | 200,000 aged `audit_log` rows, 808 statements | 7 s | **53 ms** (`DELETE ... WHERE id IN (500 ids)`) |
| CSV import (`ContactImportService.import_csv`) | 10,000 rows (the row cap, `MAX_IMPORT_ROWS`; the 5 MB byte cap is not reached: 10,000 rows is 140 KB) | 1.77 s | **53 ms** |
| Erasure, backup/verify, reconciliation, analysis queries | not measured here | | [ASSUMED] bounded per subject/batch; measured in P2-3 when limits are applied |

Conclusion for P2-3: every measured bulk operation is already batched and its slowest statement is
under 1 s, so a default statement timeout of several seconds for the API and tens of seconds for
workers leaves wide headroom. The unbatched ones above (erasure, reconciliation, analysis) must be
timed in P2-3 before limits are chosen.

## 6. Baseline load smoke [RAN]

`loadtest.py`, `WEB_CONCURRENCY=2`, 10 s per point, `GET /api/v1/campaigns?limit=50` and
`POST /api/v1/campaigns`. The mutation rate limiter (`MUTATION_RATE_LIMIT_PER_MINUTE`, default 120)
turns the write test into a 429 test, so it was raised to 100,000,000 for load runs only.

| Endpoint | Conc. | req/s | 5xx | pool timeouts | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| read | 10 | 213 | 0 | 0 | 32 ms | 122 ms | 230 ms |
| read | 50 | 132 | 0 | 0 | 277 ms | 1021 ms | 1486 ms |
| read | 100 | 110 | 0 | 0 | 688 ms | 2822 ms | 4831 ms |
| write | 10 | 143 | 0 | 0 | 59 ms | 139 ms | 291 ms |
| write | 50 | 87 | 0 | 0 | 426 ms | 1555 ms | 2465 ms |
| write | 100 | 77 | 0 | 0 | 1095 ms | 3656 ms | 5776 ms |

Reading of the baseline: with fast queries the single CPU saturates first, so latency grows and
nothing errors. The failure modes in section 4 appear only when queries stall (lock, slow statement,
dead connection). P2-8 therefore needs a slow-query variant to find the pool knee; fast queries
alone cannot.

## 7. Suite baseline (Python 3.11, local) [RAN]

`pytest -q -p no:cacheprovider` on `71b1511`: **1975 passed, 13 skipped, 0 failed**. (First run was
1965 passed / 10 failed: all in `tests/ops/test_ops_drills.py` because the `postgres` OS user could
not execute the venv's interpreter in this sandbox; after fixing file permissions the same file is
11 passed [RAN]. Not a repo defect.) Skips are lower than the 56 in the brief because `age` and the
PostgreSQL binaries are installed here.

## 8. Phase 2 implications

1. Add pool settings plus pre-ping (P2-2): removes the 5xx-per-dead-connection.
2. Map pool and connection errors to 503 + `Retry-After` (P2-2).
3. Add server-side lock/statement/idle timeouts (P2-3): bounds the 45 s hang.
4. Align threadpool (40) with pool capacity (15) and write the connection budget (P2-4).
5. Serialize migrations and stop running them from four services (P2-5).
