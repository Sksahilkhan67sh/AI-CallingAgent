# Phase 2 "Database resilience and performance" state

Integration branch: `main`. Labels: [RAN] / [READ] / [ASSUMED].

| ID | Task | Status | PR# | Evidence | Iteration |
|---|---|---|---|---|---|
| P2-0 | Recon | MERGED | #88 | `00-recon.md`; baseline 1975 passed, 13 skipped [RAN]; 5xx per dead connection, 45 s lock hang, pool timeout = 500 after 30 s, 3/3 concurrent-alembic crash all reproduced [RAN] | 1 |
| P2-1 | Carry-over hygiene (Dockerfile pins, age in CI) | MERGED | #89 | CI skip count in tests/ops 43 of 111 -> 0 of 111 (annotations on 41c1496 / 1ef54a7) [RAN]; 9 new tests failed before, pass after incl. faketime x3 [RAN]; Dockerfiles pinned python:3.11-slim / node:22-slim; Dependabot ignores added; `docker build` of both is HUMAN-TODO (no Docker): `docker build ./backend && docker build ./frontend` | 2 |
| P2-2 | Pool: dead connections, fail fast | MERGED | #93 | kill all pooled conns -> 50 requests 0 5xx (was [500,200,...]) [RAN]; pool_size=2/overflow=0/timeout=1 with 10 slow requests -> 8 x 503 in ~1 s, 2 ok [RAN]; real WorkerLoop survives the kill and dials once, 500/500 repeats [RAN]; 16 tests red before (15 failed) -> 17 pass; full suite 2000 passed, 13 skipped [RAN]; faketime x3 pass (see PR for the monotonic-clock caveat) | 3 |
| P2-3 | Statement/lock/idle timeouts | TODO | | | |
| P2-4 | Pool vs threadpool, connection budget | TODO | | | |
| P2-5 | Migrations as one-shot job | TODO | | | |
| P2-6 | Web concurrency, graceful shutdown | TODO | | | |
| P2-7 | DB-resilience gate | TODO | | | |
| P2-8 | Load smoke, capacity numbers | TODO | | | |
| P2-9 | Close-out | TODO | | | |

## Resume point
Next: start P2-3 (branch `fix/p2-3-timeouts`).

## HUMAN-TODO
- P2-1: `docker build ./backend` and `docker build ./frontend` (never built in this sandbox).
