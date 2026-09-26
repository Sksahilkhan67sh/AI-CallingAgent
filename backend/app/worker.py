"""Calling worker entrypoint -- Checkpoint 03 Step 22.

    connect dependencies -> loop { consume -> claim -> admission ->
    dial -> persist -> ack } -> graceful shutdown

Run with: python -m app.worker
"""

import logging
import signal
import socket
import uuid
from datetime import UTC, datetime
from types import FrameType

from app.core.database import SessionLocal
from app.core.redis_client import get_redis
from app.services.admin.system_service import DIALER_HEARTBEAT_KEY
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.factory import get_admission_controller, get_queue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.factory import get_telephony_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

_shutdown_requested = False

# Checkpoint 05: how often (in loop iterations) to check for recovery
# jobs that have come due -- not on every iteration, same reasoning as
# the existing stale-job-reclaim cadence below.
_RECOVERY_DISPATCH_EVERY_N_ITERATIONS = 10


def _write_heartbeat() -> None:
    try:
        from app.core.config import get_settings

        get_redis().set(
            DIALER_HEARTBEAT_KEY,
            datetime.now(UTC).isoformat(),
            ex=get_settings().worker_heartbeat_ttl_seconds,
        )
    except Exception:
        logger.exception("heartbeat_write_failed")


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown_requested = True


def run() -> None:
    """Graceful shutdown (Step 22): stops claiming new jobs as soon as a
    SIGTERM/SIGINT is received, but the job currently in flight (already
    inside process_one_job) is allowed to finish -- its persistence and
    ack are not interrupted mid-way, since that would risk an unacked
    job for work that was actually already done.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    consumer_name = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    queue = get_queue()
    admission = get_admission_controller()
    provider = get_telephony_provider()
    circuit_breaker = CircuitBreaker(get_redis(), provider.name)
    recovery_scheduler = get_recovery_scheduler()

    logger.info("worker_started", extra={"consumer_name": consumer_name})

    reclaim_counter = 0
    try:
        while not _shutdown_requested:
            db = SessionLocal()
            try:
                # Periodically reclaim jobs left stale by a crashed
                # worker (Step 21-22), not on every loop iteration.
                reclaim_counter += 1
                if reclaim_counter % 50 == 0:
                    from app.core.config import get_settings

                    reclaimed = queue.reclaim_stale(
                        consumer_name, get_settings().queue_reclaim_idle_ms
                    )
                    if reclaimed:
                        logger.info("reclaimed_stale_jobs", extra={"count": len(reclaimed)})

                if reclaim_counter % _RECOVERY_DISPATCH_EVERY_N_ITERATIONS == 0:
                    dispatched = dispatch_due_recovery_jobs(recovery_scheduler, queue)
                    if dispatched:
                        logger.info("recovery_jobs_dispatched", extra={"count": dispatched})

                outcome = process_one_job(
                    db, queue, admission, provider, circuit_breaker, consumer_name=consumer_name
                )
                db.commit()
                if outcome != JobOutcome.NO_JOB:
                    logger.info("job_processed", extra={"outcome": outcome})

                # Checkpoint 07 §25: best-effort liveness signal for the
                # admin dashboard's system page -- a lightweight addition,
                # not a new observability backend. See
                # app/services/admin/system_service.py.
                _write_heartbeat()
            except Exception:
                db.rollback()
                logger.exception("job_processing_error")
            finally:
                db.close()
    finally:
        logger.info("worker_shutting_down", extra={"consumer_name": consumer_name})
        get_redis().close()


if __name__ == "__main__":
    run()
