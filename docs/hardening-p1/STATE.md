# Phase 1 "Stop data loss" — state

Integration branch: `main` (`develop` does not contain `main`). Labels: [RAN] / [READ] / [ASSUMED].

| ID | Task | Status | PR# | Evidence | Iteration |
|---|---|---|---|---|---|
| P1-0 | Recon | DOING | | `00-recon.md`; baseline 1730 passed, 1 skipped [RAN] | 1 |
| P1-1 | Stream trim drops undelivered jobs | TODO | | 49,995 / 250,000 lost [RAN] | |
| P1-2 | Durable retry schedule in Postgres | TODO | | | |
| P1-3 | Redis durability and auth | TODO | | | |
| P1-4 | Stuck-call sweeper | TODO | | | |
| P1-5 | Dead-letter handling | TODO | | | |
| P1-6 | Reclaim cadence and per-job deadline | TODO | | | |
| P1-7 | Explicit timeouts everywhere | TODO | | | |
| P1-8 | Transcript missing vs empty | TODO | | | |
| P1-9 | Verified logic bugs | TODO | | | |
| P1-10 | Failure-injection gate | TODO | | | |
| P1-11 | Close-out | TODO | | | |
