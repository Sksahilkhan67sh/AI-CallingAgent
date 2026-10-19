# Backlog (no evidence yet)

- Analysis stream `analysis/queue.py:37` uses `MAXLEN ~ 200_000` like the dial stream. It is repaired by the Postgres analysis sweeper (`analysis/sweeper.py`) [READ]; not changed in Phase 1.
- (P1-4) A dial attempt held by a provider halt (`DOGRAH_PROVIDER_HALT_HELD`, FAILED_TO_CONNECT, never triggered) lives only as an unacked stream message; if Redis is lost it is not repaired by the sweeper (it is not INITIATED). No evidence of loss yet; needs a test before a fix.
- (P1-4) `NeedsReview` contacts are not counted in the admin dashboard / campaign stats and the erasure job treats them as in-flight. Visibility and an operator "resolve" action are product work; the audit event `sweeper.needs_review` is the record today.
- (P1-5) The dead-letter stream `<stream>:dlq` (`calls:outbound:dlq`) is unbounded and has no consumer or admin view; operators inspect it with `XRANGE`. A bounded retention policy and a "replay from DLQ" tool are product work.
- (P1-5) If PostgreSQL is down when a message is dead-lettered, the contact is not marked `NeedsReview` (the DLQ stream is the durable record); the contact stays PENDING behind its 1 h enqueue guard.
- (P1-6) Recovery dispatch is still every 10 loop iterations (`_RECOVERY_DISPATCH_EVERY_N_ITERATIONS`), so a busy worker dispatches due retries less often. No loss (the schedule is durable, P1-2); make it time-based if retry latency under load matters.
- (P1-6) `JOB_DEADLINE_SECONDS` uses SIGALRM, so it only works in the main thread of a POSIX process (the worker loop). Elsewhere it is a no-op.
- (P1-7) Database statement and lock timeouts are Phase 2 by decision and were not touched.
- (P1-7) The guard (`tests/test_p1_7_timeouts.py`) covers `httpx`, `redis`, `socket.create_connection`, `urllib.request.urlopen`, `http.client`, `smtplib` and flags any import of `requests`, `aiohttp` or `urllib3`. A new client type must be added to it.
- (P1-8) Sessions created before this change keep `transcript_status` NULL. An earlier failed fetch was stored as an empty conversation and cannot be told apart from a real one now; nothing is rewritten.
- (P1-8) `transcript_url` is a signed link retained only while a retryable-unavailable transcript awaits a retry. It is cleared on resolution, by erasure and by the new `transcript_links` retention step (same `RETENTION_TRANSCRIPT_DAYS`). If a link expires before the transcript arrives, the analysis fails with `transcript_unavailable` after its attempts.
