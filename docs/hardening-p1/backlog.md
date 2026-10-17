# Backlog (no evidence yet)

- Analysis stream `analysis/queue.py:37` uses `MAXLEN ~ 200_000` like the dial stream. It is repaired by the Postgres analysis sweeper (`analysis/sweeper.py`) [READ]; not changed in Phase 1.
