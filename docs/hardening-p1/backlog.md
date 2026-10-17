# Backlog (no evidence yet)

- Analysis stream `analysis/queue.py:37` uses `MAXLEN ~ 200_000` like the dial stream. It is repaired by the Postgres analysis sweeper (`analysis/sweeper.py`) [READ]; not changed in Phase 1.
- (P1-4) A dial attempt held by a provider halt (`DOGRAH_PROVIDER_HALT_HELD`, FAILED_TO_CONNECT, never triggered) lives only as an unacked stream message; if Redis is lost it is not repaired by the sweeper (it is not INITIATED). No evidence of loss yet; needs a test before a fix.
- (P1-4) `NeedsReview` contacts are not counted in the admin dashboard / campaign stats and the erasure job treats them as in-flight. Visibility and an operator "resolve" action are product work; the audit event `sweeper.needs_review` is the record today.
