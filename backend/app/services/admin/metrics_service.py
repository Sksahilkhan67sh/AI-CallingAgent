"""Metrics snapshot for the admin API -- Checkpoint 09.

Counters come from app.core.metrics; gauges are computed here from their
authoritative source at read time, so they cannot drift.
"""

import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.enums import AnalysisStatus, CallAttemptState


def _stream_gauges() -> dict[str, Any]:
    settings = get_settings()
    redis = get_redis()
    gauges: dict[str, Any] = {}
    try:
        gauges["queue_depth"] = redis.xlen(settings.queue_stream_key)
        gauges["dlq_size"] = redis.xlen(settings.queue_dlq_stream_key)
        oldest: Any = redis.xrange(settings.queue_stream_key, count=1)
        gauges["oldest_job_age_seconds"] = (
            max(0, int(time.time() - int(oldest[0][0].split("-")[0]) / 1000)) if oldest else 0
        )
    except Exception:
        gauges["queue_error"] = True
    return gauges


def collect_metrics(db: Session) -> dict[str, Any]:
    active = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .where(CallAttempt.state.in_((CallAttemptState.INITIATED, CallAttemptState.CONNECTED)))
    ).scalar_one()
    analysis_pending = db.execute(
        select(func.count())
        .select_from(CallAnalysis)
        .where(CallAnalysis.status == AnalysisStatus.PENDING)
    ).scalar_one()
    try:
        counters = metrics.read_counters()
    except Exception:
        counters = {}
    return {
        "counters": counters,
        "gauges": {
            "active_calls": active,
            "analysis_queue_depth": analysis_pending,
            **_stream_gauges(),
        },
    }
