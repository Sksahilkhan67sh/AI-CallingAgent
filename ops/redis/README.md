# Redis durability drill (P1-3)

Redis holds the dial stream, its consumer group and pending entries, the retry cache and live
counters. `docker-compose.yml` runs it with `--appendonly yes --appendfsync everysec`
(a crash loses at most about one second of writes), `--requirepass`, a named volume
(`redis_data`) and **no published port**. Postgres stays the source of truth (retry schedule,
contact state, kill switch), so anything Redis loses is rebuilt, but a restart should not need to.

## Setup
`cp .env.example .env` and set `REDIS_PASSWORD` to a long random value. Compose refuses to start
without it. Production must give the app a `REDIS_URL` with the password
(`redis://:<password>@host:6379/0`); with `ENVIRONMENT=production` the app refuses to boot otherwise.

## Drill
```
MODE=graceful ./ops/redis/restart_drill.sh   # docker compose restart redis
MODE=crash    ./ops/redis/restart_drill.sh   # SIGKILL, then up -d
```
It checks AOF/everysec, that unauthenticated access is refused, seeds a `drill:*` stream with a
consumer group and one pending entry plus a sorted set, restarts Redis, and asserts all of it
survived. Only `drill:*` keys are touched. Exit 0 is PASS.

## Evidence
* **[RAN]** on a real `redis-server` 7.0.15 started with the same flags (`--appendonly yes
  --appendfsync everysec --requirepass`): `graceful` PASS, `crash` (kill -9) PASS. Negative
  control: the same script against Redis without AOF exits 1 (`appendonly is not yes`).
* **HUMAN-TODO:** the drill has **not** been run through `docker compose` (no Docker in the
  environment where this was written). Run both modes once on the real stack and record the
  result in `docs/hardening-p1/STATE.md`. Also confirm `docker compose config` parses.

## Notes
* Existing volumes: a Redis that already ran without AOF starts the first AOF from the RDB it finds;
  take a snapshot first if the current contents matter.
* `everysec` is a durability/throughput trade-off. `always` removes the one-second window at a
  large throughput cost and is not needed because Postgres can rebuild what a crash loses.
