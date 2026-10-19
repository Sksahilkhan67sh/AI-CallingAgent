# Phase 1 progress

| # | Item | Status | PR | Merge commit | Gates | Open owner decisions |
|---|---|---|---|---|---|---|
| 1 | `hf2-dependencies-audit-gates` | Delivered, awaiting PR/merge | _(owner)_ | _(owner)_ | ruff, mypy, alembic check clean; pytest 1840 passed / 1 skipped (Py 3.11.17); pip-audit and `npm audit --omit=dev` clean; `npm ci` OK | see `HARDENING-HF2-NOTES.md` §10 |
| 2 | `hf3-infrastructure-hygiene` | Not started | | | | |
| 3 | `cp12d-1-durable-retry-schedule` | Not started | | | | |
| 4 | `cp12d-2-stuck-call-sweeper` | Not started | | | | |
| 5 | `cp12d-3-stream-reliability` | Not started | | | | |
| 6 | `cp12d-4-resilience-timeouts` | Not started | | | | |
| 7 | `cp12d-5-logic-bugs` | Not started | | | | |
| 8 | `cp16l-alerts-metrics` | Not started | | | | |

## Hardening phase "Stop data loss" (P1-0 to P1-11)

State, evidence and settings live in `docs/hardening-p1/` (`STATE.md`, `SETTINGS.md`,
`backlog.md`, `00-recon.md`, `P1-4-sweeper.md`, `P1-10-failure-injection.md`, `FINAL-REPORT.md`).
This delivers the items above numbered 3 to 7 (durable retry schedule, stuck-call sweeper, stream
reliability, resilience timeouts, logic bugs); items 1, 2 and 8 are unchanged by this phase.

| ID | Task | PR | Merge commit |
|---|---|---|---|
| P1-0 | Recon, state file, backlog | #73 | `bb1846c` |
| P1-1 | Never trim undelivered dial jobs; backlog backpressure | #75 | `0e29c3e` |
| P1-2 | Durable retry schedule in PostgreSQL | #76 | `cc83d09` |
| P1-3 | Redis AOF + password, production Redis-auth validator, restart drill | #77 | `26311f3` |
| P1-4 | Stuck-call sweeper (never redials), `NeedsReview` status | #78 | `7e94afe` |
| P1-5 | Dead-letter stream for poison and malformed messages | #79 | `3afe4f2` |
| P1-6 | Time-based jittered reclaim, per-job deadline | #81 | `36421dc` |
| P1-7 | Explicit timeouts from settings, AST guard | #82 | `6481f4d` |
| P1-8 | Transcript OK / EMPTY / UNAVAILABLE | #83 | `4838715` |
| P1-9 | Recovery dispatch drops stale entries | #84 | `69af8d9` |
| P1-10 | Failure-injection gate | #86 | `b394711` |
| P1-11 | Close-out | #87 | see `STATE.md` |

Suite on Python 3.11: 1730 passed, 1 skipped before, 1875 passed, 2 skipped after (the second
skip is the PostgreSQL-restart scenario, which needs `P1_PG_RESTART_CMD`).

