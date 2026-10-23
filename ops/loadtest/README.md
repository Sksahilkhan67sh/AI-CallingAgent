# Load smoke (P2-8)

A single-host smoke test for the API. **Not a capacity claim**: one host, the load generator on the
same CPU, a mock provider (nothing dials). Numbers and caveats: `docs/hardening-p2/LOAD-SMOKE.md`.

```
cd backend
# PostgreSQL and Redis running, database migrated, then:
export PRIMARY_DB_URL=... REDIS_URL=...
python ../ops/loadtest/loadtest.py --endpoint slow --slow-ms 500 --concurrency 100 --seconds 20
sh ../ops/loadtest/run_matrix.sh after . > after.txt                 # the whole matrix
```

* `loadtest.py` starts the API the way production does (`python -m app.serve`; plain `uvicorn`
  for a checkout older than the launcher), applies N concurrent clients for T seconds to one
  endpoint (`read`, `write` or `slow`) and prints one line: status counts, pool-timeout count and
  p50/p95/p99. `--env KEY=VALUE` sets a server environment variable (for example
  `API_THREADPOOL_SIZE=60`).
* `slow_app.py` is the real API plus `GET /_db_slow?ms=N`, a route that holds a database
  connection for N ms with a server-side sleep. Fast queries saturate the CPU before they touch
  the pool; this is what shows pool and threadpool behaviour. Load tests only.
* `run_matrix.sh LABEL [BACKEND_DIR] [STAGES]` runs the stages `fast`, `slow` and `slow2000`.

To get a real "before", check out the earlier commit into a worktree
(`git worktree add /tmp/base <sha>`) and pass `/tmp/base/backend` as `BACKEND_DIR`.
