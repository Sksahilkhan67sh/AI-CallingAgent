# Phase 2 backlog (found, out of scope, not fixed)

| Found in | Item | Why it is not fixed here |
|---|---|---|
| P2-2 | libfaketime and the timing tests: under `faketime -f "@..."` timed condition waits (`queue.get(timeout=...)`, the pool timeout) never return; with `FAKETIME_DONT_FAKE_MONOTONIC=1` they work but `time.sleep` raises EINVAL. A pool-timeout test therefore cannot pass in both modes; `tests/test_p2_2_pool_resilience.py` is run with `DONT_FAKE_MONOTONIC` and skips its asyncio timing test under faketime, and its `_kill` helper's `time.sleep` is only safe because the killed backends are already gone on the first check. | Test-tooling limitation, not product behaviour. |
| P2-1 | `ubuntu-latest` is migrating to Ubuntu 26 (runner notice, 2026-10-19). The `apt-get install age shellcheck` step and the `/usr/lib/postgresql/16` paths used by the ops drills may break. | Needs a CI run on the new image to know. |
| P2-1 | `docker build` of both Dockerfiles has never been run in this sandbox. | HUMAN-TODO. |
| P2-0 | Worker / analysis-worker may spin in a tight error loop while the database is down for a long time (no backoff in the catch-all). Not measured yet. | Measured in P2-7 before any change. |
| P2-3 | `reconcile_suppressions` and per-subject erasure were not timed at volume. | Both run under the `script` role (60 s); revisit with real data. |
