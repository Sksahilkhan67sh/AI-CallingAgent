# Phase 2 "Database resilience and performance" state

Integration branch: `main`. Labels: [RAN] / [READ] / [ASSUMED].

| ID | Task | Status | PR# | Evidence | Iteration |
|---|---|---|---|---|---|
| P2-0 | Recon | MERGED | #88 | `00-recon.md`; baseline 1975 passed, 13 skipped [RAN]; 5xx per dead connection, 45 s lock hang, pool timeout = 500 after 30 s, 3/3 concurrent-alembic crash all reproduced [RAN] | 1 |
| P2-1 | Carry-over hygiene (Dockerfile pins, age in CI) | TODO | | | |
| P2-2 | Pool: dead connections, fail fast | TODO | | | |
| P2-3 | Statement/lock/idle timeouts | TODO | | | |
| P2-4 | Pool vs threadpool, connection budget | TODO | | | |
| P2-5 | Migrations as one-shot job | TODO | | | |
| P2-6 | Web concurrency, graceful shutdown | TODO | | | |
| P2-7 | DB-resilience gate | TODO | | | |
| P2-8 | Load smoke, capacity numbers | TODO | | | |
| P2-9 | Close-out | TODO | | | |

## Resume point
Next: start P2-1 (branch `fix/p2-1-hygiene`).
