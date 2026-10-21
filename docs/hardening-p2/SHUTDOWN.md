# Web concurrency and graceful shutdown (P2-6)

## Decision: `uvicorn --workers`, not gunicorn + uvicorn workers
| | uvicorn `--workers` (chosen) | gunicorn + `UvicornWorker` |
|---|---|---|
| New dependency | none | gunicorn: new pin in `constraints.txt` (regenerated on Python 3.11), new surface for the dependency-audit job |
| Multiple processes | yes (uvicorn 0.54 supervisor) | yes |
| Graceful-shutdown timeout | `--timeout-graceful-shutdown` | `--graceful-timeout` |
| Keep-alive timeout | `--timeout-keep-alive` | `--keep-alive` |
| Dead-worker restart | yes | yes |
| Recycling after N requests | `--limit-max-requests` (+ jitter) | `--max-requests` |
What gunicorn adds (pre-fork tuning, hooks) is not needed here, and a dependency change would have
spent the phase's "no unrelated bumps" rule. Revisit if worker recycling hooks are ever needed.

The API starts with `python -m app.serve` (`app/serve.py`): it reads `WEB_CONCURRENCY`,
`WEB_GRACEFUL_SHUTDOWN_SECONDS` and `WEB_KEEP_ALIVE_SECONDS` and calls `uvicorn.run`. Each worker
process imports the app, so each has its own engine, pool and threadpool: the connection budget
counts `WEB_CONCURRENCY` processes for the API (`CONNECTION-BUDGET.md`).

## SIGTERM behaviour
| Process | On SIGTERM | Evidence |
|---|---|---|
| API (`app.serve`) | stops accepting; in-flight requests finish for up to `WEB_GRACEFUL_SHUTDOWN_SECONDS` (30), then the process stops; exit 0 | tests: four 3 s requests in flight during SIGTERM all returned 200, exit 0, new connections refused; a 30 s request with a 2 s grace period did not delay exit beyond the grace period [RAN] |
| worker | sets a flag; finishes the job in flight (up to the job deadline) and then leaves its loop; never starts another | [READ] `app/worker.py:75-80,183-189`; idle exit [RAN] |
| analysis worker | same | [READ] `app/analysis_worker.py:56-67`; idle exit [RAN] |
| sweeper | sets a flag; finishes the pass in flight; sleeps in 1 s slices so it exits within about a second | [READ] `app/sweeper.py:22-54`; idle exit [RAN] |

If a worker is killed instead (SIGKILL, grace period exceeded), the job it held is left un-acked and
Phase 1's reclaim and sweeper recover it; the "never redial when unsure" rule still applies. No code
change was made to the background processes: no data loss and no hang was found.

## Container stop
`docker stop` sends SIGTERM and kills after `stop_grace_period`. Compose sets it above each
process's own shutdown time: `backend` 40 s (> 30 s graceful), `worker` and `analysis-worker` 40 s
(> the 25 s job deadline), `sweeper` 20 s. A test checks the relation.

## Not verified here
The behaviour through Docker itself (`docker compose stop`, the 40 s window): no Docker in the
sandbox. HUMAN-TODO: `docker compose up -d && docker compose stop backend` while a slow request is
in flight and confirm it completes and the container exits 0 before the grace period.
