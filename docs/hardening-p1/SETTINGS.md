# Phase 1 settings added

| Setting (env) | Default | Task | Production recommendation |
|---|---|---|---|
| `QUEUE_MAX_BACKLOG` | 200000 | P1-1 | Max dial jobs in the Redis stream. One job is about 511 bytes, so the default is about 100 MB. Size it to Redis `maxmemory` with headroom; excess contacts stay PENDING in Postgres. |
| `RETRY_REBUILD_INTERVAL_SECONDS` | 60 | P1-2 | Keep 60. Lower only if Redis loss must heal faster; each run is one indexed query. |
| `RETRY_REBUILD_BATCH_SIZE` | 500 | P1-2 | Keep 500 (rows locked per page). |
| `RETRY_REDISPATCH_SECONDS` | 900 | P1-2 | Must exceed the longest normal wait between dispatch and dial claim; 900 is safe for the default queue depth. |
