"""Minimal operational counters -- Checkpoint 09.

Redis hash of monotonically increasing counters, written best-effort:
a metrics failure must never affect call handling. Gauges (queue depth,
DLQ size, active calls) are computed on read from their real sources in
the admin metrics endpoint, not stored here. Redis holds only operational
telemetry here, never business state.
"""

import logging

from app.core.redis_client import get_redis

logger = logging.getLogger("metrics")

_KEY = "metrics:counters"

# Names used across the codebase, kept in one place to prevent typos.
CALLS_INITIATED = "calls_initiated"
CALLS_CONNECTED = "calls_connected"
CALLS_FAILED = "calls_failed"
CALLS_COMPLETED = "calls_completed"
CALLS_PARTIAL = "calls_partial"
RETRY_COUNT = "retry_count"
WEBHOOK_COUNT = "webhook_count"
WEBHOOK_DUPLICATES = "webhook_duplicates"
WEBHOOK_FAILURES = "webhook_failures"
DOGRAH_ERRORS = "dograh_errors"
WORKER_FAILURES = "worker_failures"
STALE_JOBS = "stale_jobs"
DLQ_ENTRIES = "dlq_entries"
OPT_OUT_COUNT = "opt_out_count"


def incr(name: str, amount: int = 1) -> None:
    try:
        get_redis().hincrby(_KEY, name, amount)
    except Exception:
        logger.warning("metric_write_failed", extra={"metric": name})


def read_counters() -> dict[str, int]:
    raw = get_redis().hgetall(_KEY)
    return {k: int(v) for k, v in raw.items()}  # type: ignore[union-attr]
