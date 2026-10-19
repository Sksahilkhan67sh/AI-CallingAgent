# Phase 1 "Stop data loss": final report

| Task | PR | Status |
|---|---|---|
| P1-0 recon | #73 | MERGED |
| P1-1 stream trim / backpressure | #75 | MERGED |
| P1-2 durable retry schedule | #76 | MERGED |
| P1-3 Redis AOF + auth | #77 | MERGED |
| P1-4 stuck-call sweeper | #78 | MERGED |
| P1-5 dead-letter stream | #79 | MERGED |
| P1-6 reclaim cadence + deadline | #81 | MERGED |
| P1-7 explicit timeouts | #82 | MERGED |
| P1-8 transcript OK/EMPTY/UNAVAILABLE | #83 | MERGED |
| P1-9 logic bugs (dispatch stale entries) | #84 | MERGED |
| P1-10 failure-injection gate | #86 | MERGED |
| P1-11 close-out | #87 | MERGED |

**Full suite, Python 3.11:** before 1730 passed, 1 skipped; after 1875 passed, 2 skipped (+145 tests).
**Failure injection [RAN]:** 9 passed (kill at 3 dial steps, FLUSHALL, backlog, duplicate delivery, poison,
concurrent sweepers/rebuilders), also under faketime 17:29:30 / 05:00 / 12:00; 200 contacts; 250,000-job
backlog; real PostgreSQL restart mid-run recovered after reconnect (1 failed step, then exactly-once
dials). Four mutation checks (sweeper, rebuild, MAXLEN, DLQ disabled) each fail the gate.
**Settings added (16 + `REDIS_PASSWORD`):** see `SETTINGS.md` for defaults and production advice.
**HUMAN-TODO:** (1) run `ops/redis/restart_drill.sh` (graceful and crash) through `docker compose` and
confirm `docker compose config` parses; (2) set `REDIS_PASSWORD` and a password-bearing production
`REDIS_URL`; (3) run `P1_PG_RESTART_CMD=... pytest tests/test_p1_failure_injection.py -k postgres_restart`
against your own stack if you want that scenario in your pipeline; (4) rotate the GitHub token that was
supplied for this work.
**Remaining risks:** a dial slower than `JOB_DEADLINE_SECONDS` (25 s; worst case 60 s) is abandoned and
resolved without redial; a call that was never placed is not retried automatically (an operator reopens
`NeedsReview`); the dead-letter stream is unbounded with no admin view or replay tool; `NeedsReview` is not
in dashboard counts; the native/mock engine commits its claim after the call (production uses Dograh);
the analysis stream still uses `MAXLEN ~` (repaired by its Postgres sweeper); recovery dispatch is still
iteration-based; transcripts that failed before P1-8 cannot be told from empty ones. Details: `backlog.md`.
**Process note:** PR #82 was merged before its checks had registered (the helper treated "no checks
reported" as green). Its checks and `main`'s later passed; the helper is now strict.
Phase 1 is complete only when P1-10 passes in CI on the integration branch.
