"""System health -- Checkpoint 07 §25-26.

No new observability backend -- reads the same Postgres/Redis
connections the app already uses, plus a best-effort worker heartbeat
(a Redis key each worker loop touches once per iteration; see
app/worker.py and app/analysis_worker.py). A missing/expired heartbeat
means "no worker touched this key recently", not a proven crash -- this
approximation is documented, not hidden.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.schemas.admin import ComponentHealth, QueueHealth, SystemHealthResponse
from app.services.analysis.factory import get_analysis_queue
from app.services.queue.factory import get_queue

DIALER_HEARTBEAT_KEY = "heartbeat:worker"
ANALYSIS_HEARTBEAT_KEY = "heartbeat:analysis_worker"


def _check_postgres(db: Session) -> ComponentHealth:
    try:
        db.execute(text("SELECT 1"))
        return ComponentHealth(name="PostgreSQL", status="ok")
    except Exception as exc:
        return ComponentHealth(name="PostgreSQL", status="degraded", detail=str(exc)[:200])


def _check_redis() -> ComponentHealth:
    try:
        get_redis().ping()
        return ComponentHealth(name="Redis", status="ok")
    except Exception as exc:
        return ComponentHealth(name="Redis", status="degraded", detail=str(exc)[:200])


def _check_worker(name: str, heartbeat_key: str) -> ComponentHealth:
    try:
        last = get_redis().get(heartbeat_key)
    except Exception as exc:
        return ComponentHealth(name=name, status="unknown", detail=str(exc)[:200])
    if last is None:
        return ComponentHealth(
            name=name, status="unknown", detail="No heartbeat observed yet"
        )
    return ComponentHealth(name=name, status="ok", detail=f"Last heartbeat: {last}")


def _queue_health(name: str, redis_client, stream_key: str, group: str) -> QueueHealth:
    try:
        summary = redis_client.xpending(stream_key, group)
        pending = int(summary["pending"]) if summary else 0
        oldest_seconds: float | None = None
        if pending:
            detail = redis_client.xpending_range(stream_key, group, "-", "+", 1)
            if detail:
                oldest_seconds = detail[0]["time_since_delivered"] / 1000
        return QueueHealth(
            name=name,
            pending=pending,
            processing=pending,
            oldest_pending_seconds=oldest_seconds,
        )
    except Exception:
        return QueueHealth(name=name, pending=0, processing=0, oldest_pending_seconds=None)


def get_system_health(db: Session) -> SystemHealthResponse:
    components = [
        ComponentHealth(name="API", status="ok"),
        _check_postgres(db),
        _check_redis(),
        _check_worker("Dialer Worker", DIALER_HEARTBEAT_KEY),
        _check_worker("Analysis Worker", ANALYSIS_HEARTBEAT_KEY),
    ]
    outbound_queue = get_queue()
    analysis_queue = get_analysis_queue()
    queues = [
        _queue_health(
            "Outbound calls",
            outbound_queue.redis,
            outbound_queue.stream_key,
            outbound_queue.group,
        ),
        _queue_health(
            "Post-call analysis",
            analysis_queue.redis,
            analysis_queue.stream_key,
            analysis_queue.group,
        ),
    ]
    return SystemHealthResponse(components=components, queues=queues)
