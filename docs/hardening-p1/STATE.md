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
| P1-6 | Reclaim cadence and per-job deadline | PR-OPEN | #81 | 12/12 new tests error on unfixed code, pass after incl. faketime x3 [RAN]; suite 1800 passed | 7 |
| P1-7 | Explicit timeouts everywhere | TODO | | | |
| P1-8 | Transcript missing vs empty | TODO | | | |
| P1-9 | Verified logic bugs | TODO | | | |
| P1-10 | Failure-injection gate | TODO | | | |
| P1-11 | Close-out | TODO | | | |
