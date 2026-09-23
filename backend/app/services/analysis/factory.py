"""Wiring for the analysis queue -- mirrors app/services/queue/factory.py
and app/services/recovery/factory.py: one place that reads Settings into
the concrete Redis-backed object. Not cached as a singleton for the same
reason as RedisStreamQueue -- constructing it is cheap (one idempotent
XGROUP CREATE), and not caching means a stream/group wiped by a test's
FLUSHDB is transparently recreated rather than silently breaking a
supposedly process-lifetime singleton.
"""

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.services.analysis.queue import AnalysisQueue


def get_analysis_queue() -> AnalysisQueue:
    settings = get_settings()
    return AnalysisQueue(
        get_redis(), settings.analysis_stream_key, settings.analysis_consumer_group
    )
