# FINDINGS

F1 [RAN] The PAT was pasted in plain text into the session upload; treat as compromised (see 01).
F2 [READ] docs/PHASE1-PROGRESS.md:5 says HF2 awaiting merge but PR #56 is merged in main.
F3 [RAN] A "temporary" CI DIAGNOSTIC commit (9e3d13e) sits in main history.
F4 [RAN] On Python 3.11.15 the backend suite equals the 3.12 baseline: 1730 passed, 1 skipped; tests/ops 110 passed once `age`, `shellcheck` and postgres-user access exist. No product failure.
F5 [READ] ci.yml runs tests/ops without installing `age`; ~43 drill/safety tests probably skip silently in CI (ci.yml has no `age` step). Phase 1 may want to install it so backup/erasure safety tests really run.
F6 [RAN] ops/backup/drill.sh re-execs as the `postgres` user when run as root; fails with exit 126 if that user cannot read the repo/python. Affects anyone running drills in containers as root.
F7 [RAN] Docker builds not verified (no Docker in sandbox).
