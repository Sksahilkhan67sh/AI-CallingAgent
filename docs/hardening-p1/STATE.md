# Phase 1 "Stop data loss" — state

Integration branch: `main` (`develop` does not contain `main`). Labels: [RAN] / [READ] / [ASSUMED].

| ID | Task | Status | PR# | Evidence | Iteration |
|---|---|---|---|---|---|
| P1-0 | Recon | MERGED | #73 | `00-recon.md`; baseline 1730 passed, 1 skipped [RAN] | 1 |
| P1-1 | Stream trim drops undelivered jobs | MERGED | #75 | 49,995 / 250,000 lost before; 0 lost after at 250k [RAN]; suite 1736 passed, 1 skipped | 2 |
| P1-2 | Durable retry schedule in Postgres | MERGED | #76 | 8/8 new tests failed before (retry lost `0 == 1`, double dispatch) [RAN] | 3 |
| P1-3 | Redis durability and auth | MERGED | #77 | 10/15 new tests failed before [RAN]; drill PASS graceful+crash on real redis-server 7.0.15 with the compose flags [RAN]; compose drill HUMAN-TODO (no Docker) | 4 |
| P1-4 | Stuck-call sweeper | MERGED | #78 | stuck state reproduced at 3 death points on unfixed code; 13 sweeper tests failed (no module) [RAN]; 16 pass incl. faketime x3 | 5 |
| P1-5 | Dead-letter handling | MERGED | #79 | poison/malformed behaviour reproduced on unfixed code [RAN]; 9 tests pass incl. faketime x3; suite 1788 passed | 6 |
| P1-6 | Reclaim cadence and per-job deadline | MERGED | #81 | 12/12 new tests error on unfixed code, pass after incl. faketime x3 [RAN]; suite 1800 passed | 7 |
| P1-7 | Explicit timeouts everywhere | MERGED | #82 | AST guard found 3 real violations on unfixed code (redis_client.py:17 x2, transcript_fetch.py:44) [RAN]; 33 tests pass incl. faketime x3; suite 1833 passed | 8 |
| P1-8 | Transcript missing vs empty | MERGED | #83 | failed fetch == empty transcript (`[] == []`) shown on unfixed code [RAN]; 28 tests pass incl. faketime x3; suite 1861 passed | 9 |
| P1-9 | Verified logic bugs | MERGED | #84 | group A: dispatcher dispatched stale entries (4 failing tests on unfixed code [RAN]); suite 1866 passed; remaining suspicions in backlog.md without evidence | 10 |
| P1-10 | Failure-injection gate | DOING | | 9 passed + PG-restart scenario [RAN]; 4 mutation checks fail the gate [RAN]; 250k backlog passes [RAN]; faketime x3; suite 1875 passed, 2 skipped | 11 |
| P1-11 | Close-out | TODO | | | |
