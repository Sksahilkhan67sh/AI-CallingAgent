"""Wiring for queue/admission components, shared by API routes, the
worker entrypoint, and tests -- one place that reads Settings into the
concrete Redis-backed objects.

Not cached as singletons: `get_redis()` (the actual connection pool) is
cached, but constructing a `RedisStreamQueue`/`AdmissionController` is
cheap (one idempotent XGROUP CREATE, no other I/O), and NOT caching them
means a stream/group wiped by a test's `FLUSHDB` gets transparently
recreated on the next call rather than silently breaking a supposedly
process-lifetime singleton.
"""

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.redis_queue import RedisStreamQueue


def get_queue() -> RedisStreamQueue:
    settings = get_settings()
    return RedisStreamQueue(get_redis(), settings.queue_stream_key, settings.queue_consumer_group)


def get_admission_controller() -> AdmissionController:
    settings = get_settings()
    return AdmissionController(
        get_redis(),
        global_cps_limit=settings.global_cps_limit,
        campaign_cps_limit=settings.campaign_cps_limit,
        provider_cps_limit=settings.provider_cps_limit,
        global_concurrency_limit=settings.global_concurrency_limit,
        campaign_concurrency_limit=settings.campaign_concurrency_limit,
        provider_concurrency_limit=settings.provider_concurrency_limit,
    )
