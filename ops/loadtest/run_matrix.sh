#!/bin/sh
# Run the P2-8 load matrix against one checkout. POSIX sh.
#
#   cd backend && sh ../ops/loadtest/run_matrix.sh LABEL [BACKEND_DIR] [STAGES] > results.txt
#
# Needs PRIMARY_DB_URL / REDIS_URL set, PostgreSQL and Redis running, the database migrated.
# One line per run. STAGES is a space-separated subset of: fast slow slow2000 (default: all).
#   fast     read and write at concurrency 10/50/100, 10 s each
#   slow     a request that holds a connection 500 ms, at 10/50/100/200, 20 s each
#   slow2000 the same held for 2000 ms, at 100/200/400, 30 s each (finds where the pool
#            timeout starts to fire: the queue wait exceeds DB_POOL_TIMEOUT)
set -eu
LABEL="${1:?label}"
BACKEND_DIR="${2:-.}"
STAGES="${3:-fast slow slow2000}"
TOOL="$(dirname "$0")/loadtest.py"

for stage in $STAGES; do
  case "$stage" in
    fast)
      for ep in read write; do
        for c in 10 50 100; do
          python "$TOOL" --endpoint "$ep" --concurrency "$c" --seconds 10 --label "$LABEL" \
            --backend-dir "$BACKEND_DIR"
        done
      done ;;
    slow)
      for c in 10 50 100 200; do
        python "$TOOL" --endpoint slow --slow-ms 500 --concurrency "$c" --seconds 20 \
          --label "$LABEL" --backend-dir "$BACKEND_DIR"
      done ;;
    slow2000)
      for c in 100 200 400; do
        python "$TOOL" --endpoint slow --slow-ms 2000 --concurrency "$c" --seconds 30 \
          --label "$LABEL" --backend-dir "$BACKEND_DIR"
      done ;;
    *) echo "unknown stage: $stage" >&2; exit 2 ;;
  esac
done
