# P2-7: the database-resilience gate

`backend/tests/test_p2_db_resilience.py` runs in the normal `pytest` run (so CI executes it) against
real PostgreSQL and Redis. `backend/tests/resilience_invariants.py` is the invariant checker:
after a failure scenario it verifies **no duplicate dial** (a number is never placed twice) and
**no lost job** (every contact has a durable attempt, or its job is still queued).

Sizes come from the environment with small CI defaults: `P2_GATE_JOBS` (4),
`P2_GATE_API_REQUESTS` (20), `P2_GATE_POOL_REQUESTS` (10). `P2_PG_RESTART_CMD` enables scenario (e).

| | Scenario | Result [RAN] |
|---|---|---|
| (a) | The application's own database connections are killed **inside every provider call** (a real call is placed, then the outcome commit has nowhere to go). Jobs stay un-acked; after the claims age, recovery classifies each `AMBIGUOUS_RECOVERY`, never redials; the invariant checker finds nothing. | pass |
| (a) | After a kill: the API answers 20/20 requests with 200, the sweeper does not raise, the worker's first step dials | pass |
| (a) | The checker flags a duplicate dial and a lost job when they are injected (it can fail) | pass |
| (b) | Pool 2 / no overflow / timeout 1 s behind the real launcher, 10 concurrent 3 s requests: 2 x 200, 8 x 503 with `Retry-After`, none hangs | pass |
| (c) | A statement over the limit is cancelled and the API answers 503; a row lock held elsewhere fails within `lock_timeout`; a leaked transaction is terminated by the idle limit | pass |
| (d) | Two concurrent `alembic upgrade head` on a fresh database both exit 0 and reach one head | pass |
| (e) | A real PostgreSQL restart (`P2_PG_RESTART_CMD="pg_ctlcluster 16 main restart -m fast"`): the API recovers without an app restart, never answers a 5xx other than 503 while the server is down, then 20/20 x 200 | pass (sandbox server; skipped in CI where the variable is unset) |
| (f) | The Phase 1 failure-injection gate file is byte-identical to when Phase 2 began (pinned SHA-256) and still passes (run as a sub-process) | pass |

## Mutation checks (`ops/resilience/mutation_check.py`) [RAN]
Each mutation edits one source file in place, runs the gate, and **must make it fail**:

| Mutation | Gate | Caught by |
|---|---|---|
| disable `pool_pre_ping` | FAILS | `test_after_a_kill_the_worker_the_api_and_the_sweeper_keep_working` |
| remove the statement timeout | FAILS | `test_a_statement_over_the_limit_is_cancelled_and_the_api_answers_503` |
| remove the migration advisory lock | FAILS | `test_two_concurrent_migrators_both_succeed_and_reach_a_single_head` |
| remove the 503 mapping | FAILS | `test_an_exhausted_pool_answers_503_...` and `test_a_statement_over_the_limit_...` |

Baseline (unmutated) passed first and the tree was restored afterwards (`restored: gate exit 0`).

## Bugs the gate work found in itself (kept as lessons)
* The invariant checker compared string ids from a test helper with UUIDs from the database and
  reported every contact as lost. The mid-dial scenario is therefore also the checker's positive
  control: it feeds string ids against real attempt rows and must report nothing.
* A scenario that aged call attempts with the database's `now()` failed under `faketime` because
  the application's clock is the one faketime moves. It now ages rows relative to the app clock,
  and only for its own campaign (it first updated every attempt in the database).
* Scenarios now delete everything they create: leftover `Dialing` / `RetryScheduled` contacts
  change what a real worker does in other tests.

## faketime
`faketime -f "@..."` runs at 17:29:30, 05:00:00 and 12:00:00 (exact command): 9 passed, 1 skipped.
Two children run on the real clock because libfaketime stalls timed waits: the API server
subprocess of scenario (b) (its 1 s pool timeout never fires) and the nested Phase 1 pytest
(that file itself hangs under the default faketime). They are the system under test, not the
code whose time-of-day independence faketime checks.

## Repeats
See STATE.md for the counts actually run. The whole file takes about 30 s, so 200 repeats of every
test (about 90 minutes) was not run: the light, random-content scenarios were repeated 200 times
and the sub-process scenarios fewer times.
