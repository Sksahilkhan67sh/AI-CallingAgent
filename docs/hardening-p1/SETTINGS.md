# Phase 1 settings added

| Setting (env) | Default | Task | Production recommendation |
|---|---|---|---|
| `QUEUE_MAX_BACKLOG` | 200000 | P1-1 | Max dial jobs in the Redis stream. One job is about 511 bytes, so the default is about 100 MB. Size it to Redis `maxmemory` with headroom; excess contacts stay PENDING in Postgres. |
| `RETRY_REBUILD_INTERVAL_SECONDS` | 60 | P1-2 | Keep 60. Lower only if Redis loss must heal faster; each run is one indexed query. |
| `RETRY_REBUILD_BATCH_SIZE` | 500 | P1-2 | Keep 500 (rows locked per page). |
| `RETRY_REDISPATCH_SECONDS` | 900 | P1-2 | Must exceed the longest normal wait between dispatch and dial claim; 900 is safe for the default queue depth. |
| `REDIS_PASSWORD` (compose / root `.env`) | none, required | P1-3 | Long random value; compose refuses to start without it. Put it in the app's `REDIS_URL` (`redis://:<password>@host:6379/0`). |
| `REDIS_URL` in production | n/a | P1-3 | Must carry a password or the app refuses to boot (`ENVIRONMENT=production`). |
| `SWEEPER_STUCK_AFTER_SECONDS` | 3600 | P1-4 | Must exceed the longest legitimate call plus webhook delay, or live calls are flagged. Keep it well above Dograh's max call duration. |
| `SWEEPER_INTERVAL_SECONDS` | 60 | P1-4 | Keep 60; each pass is one indexed, batch-limited query. |
| `SWEEPER_BATCH_LIMIT` | 100 | P1-4 | Rows locked per page; the process loops until a page is empty. |
| `QUEUE_MAX_DELIVERIES` | 10 | P1-5 | Deliveries (reclaim cycles) before a never-settled job is dead-lettered. Higher = a long database outage keeps being retried; lower = poison is isolated sooner. Deliberate holds do not count. |
| `QUEUE_RECLAIM_INTERVAL_SECONDS` | 15 | P1-6 | Wall-clock seconds between reclaims of a dead worker's pending jobs (replaces "every 50 iterations"). Worst-case wait for a stranded job is this plus `QUEUE_RECLAIM_IDLE_MS` plus one job. |
| `QUEUE_RECLAIM_JITTER_RATIO` | 0.2 | P1-6 | +/- fraction applied to the interval so a fleet does not reclaim in lockstep. Keep 0.1 to 0.3. |
| `JOB_DEADLINE_SECONDS` | 25 | P1-6 | Abandon any one job after this long. Must be shorter than `QUEUE_RECLAIM_IDLE_MS / 1000` (validated). A worst-case dial (trigger + two lookups) can take up to 3 x (connect + read) = 60 s; if you need to allow that, raise `QUEUE_RECLAIM_IDLE_MS` first, then the deadline. 0 disables. |
