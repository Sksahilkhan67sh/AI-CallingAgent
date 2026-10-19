# P1-10 failure-injection gate

`backend/tests/test_p1_failure_injection.py` runs in CI with the rest of the suite (about 35 s).
Each scenario injects a failure into the real pipeline (`WorkerLoop`, dialer, queue, recovery,
sweeper) and then runs one invariant checker over PostgreSQL and Redis:

1. no duplicate dial: every (contact, attempt number) was triggered at most once;
2. no job lost: a PENDING contact whose enqueue guard exists has its job on the stream; a
   RETRY_SCHEDULED contact has a durable due time;
3. every contact is terminal, or waiting in a recoverable way; the `final` check allows terminal only;
4. no attempt in flight beyond the stuck threshold.

| Scenario | Injected failure |
|---|---|
| kill the worker at each dial step | before the trigger, after the trigger before the id is saved, after commit before ack |
| Redis FLUSHALL mid-campaign | stream, group, guards and the retry cache gone |
| backlog | far more contacts than the stream cap, no consumer |
| duplicate delivery | every job delivered twice |
| poison message | an always-raising job and an unreadable payload |
| concurrent sweepers and rebuilders | two of each over stuck attempts and a flushed retry cache |
| PostgreSQL restart mid-run | real server restart (needs `P1_PG_RESTART_CMD`) |

Sizes: `P1_FI_CONTACTS` (default 12), `P1_FI_BACKLOG` (default 300). Time is injected (fake loop
clock, explicit sweeper `now`; attempt start times are anchored to the Python clock), so the file
also passes under `faketime -f "@2026-10-16 17:29:30"`.

## Results

| Run | Result |
|---|---|
| CI-size defaults, Python 3.11 | 9 passed, 1 skipped (PG restart needs the env var) [RAN] |
| `faketime` 17:29:30, 05:00, 12:00 UTC | 9 passed, 1 skipped each [RAN] |
| `P1_FI_CONTACTS=200` | 9 passed, 1 skipped, 83 s [RAN] |
| `P1_FI_BACKLOG=250000` (backlog scenario) | passed, 115 s: the stream holds exactly the cap, the other contacts are un-enqueued and enqueueable [RAN] |
| `P1_PG_RESTART_CMD="pg_ctlcluster 16 main restart"` | passed: 1 loop step failed while the database was down, then the pool reconnected; every contact was dialed exactly once and completed [RAN] |

**PostgreSQL restart, documented behaviour:** while the database is down a loop step fails and rolls
back (the loop never dies, the unacked job stays pending); after the restart the pool reconnects
and the run completes. Recovery is "after reconnect", not transparent.

## The gate can fail (mutation checks, all [RAN])
Each Phase 1 fix was broken on purpose and the gate failed: sweeper a no-op -> the kill scenarios
and the concurrent-sweeper scenario fail; rebuild a no-op -> the FLUSHALL and concurrent scenarios
fail; `MAXLEN` back on `XADD` -> the backlog scenario fails; dead-lettering disabled -> the poison
scenario fails. `test_the_checker_flags_each_invariant` also asserts each invariant can be violated.

## Phase 1 is complete only when this file passes in CI on the integration branch.
