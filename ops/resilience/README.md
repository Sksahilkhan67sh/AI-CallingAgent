# Resilience gate tooling (P2-7)

`mutation_check.py` proves `backend/tests/test_p2_db_resilience.py` can fail: it removes pool
pre-ping, the statement timeout, the migration advisory lock and the 503 mapping one at a time
and requires the gate to fail each time. Run it from `backend/` with PostgreSQL and Redis up
(same environment as the test suite). It restores the edited file from memory in a `finally`, so
an interrupted run leaves the tree as it was except in the unlikely event of a hard kill: check
`git diff` afterwards.
