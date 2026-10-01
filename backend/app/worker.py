"""Calling worker entrypoint -- Checkpoint 03 Step 22.

    connect dependencies -> loop { consume -> claim -> admission ->
    dial -> persist -> ack } -> graceful shutdown

Run with: python -m app.worker
"""

import logging
import signal
import socket
import time
import uuid
from datetime import UTC, datetime
from types import FrameType

from app.core import metrics
from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.core.logging_config import configure_logging
from app.core.redis_client import get_redis
from app.services.admin.system_service import DIALER_HEARTBEAT_KEY
from app.services.queue.dialer_worker import (
    JobOutcome,
    admission_provider_name,
    process_one_job,
    process_reclaimed,
)
from app.services.queue.factory import get_admission_controller, get_queue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.dograh_client import DograhConfigurationError
from app.services.telephony.dograh_reconciler import (
    readmit_missing_analysis,
    reconcile_stale_attempts,
)
from app.services.telephony.factory import get_dograh_client, get_telephony_provider

configure_logging()
logger = logging.getLogger("worker")

_shutdown_requested = False

# Checkpoint 05: how often (in loop iterations) to check for recovery
# jobs that have come due.
_RECOVERY_DISPATCH_EVERY_N_ITERATIONS = 10
# Checkpoint 09: time-based cadences (a busy queue must not make these hot).
_RECLAIM_EVERY_SECONDS = 15.0
_RECONCILE_EVERY_SECONDS = 30.0
_MAX_ERROR_BACKOFF_SECONDS = 30.0


def _write_heartbeat() -> None:
    try:
        get_redis().set(
            DIALER_HEARTBEAT_KEY,
            datetime.now(UTC).isoformat(),
            ex=get_settings().worker_heartbeat_ttl_seconds,
        )
    except Exception:
        logger.warning("heartbeat_write_failed")


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown_requested = True


def _sleep_interruptibly(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _shutdown_requested and time.monotonic() < deadline:
        time.sleep(0.2)


def run() -> None:
    """Graceful shutdown: on SIGTERM/SIGINT the worker stops taking new
    jobs but the job already inside process_one_job finishes (its commit
    and ack are never interrupted), then DB and Redis connections close.
    A job that was read but not completed stays pending in the stream and
    is reclaimed by another worker; reclaim is safe because the attempt row
    carrying the provider-intent marker is committed before any call.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    consumer_name = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    queue = get_queue()
    admission = get_admission_controller()
    provider = get_telephony_provider()
    circuit_breaker = CircuitBreaker(get_redis(), admission_provider_name(provider))
    recovery_scheduler = get_recovery_scheduler()
    settings = get_settings()

    dograh_client = None
    if settings.calling_engine == "dograh":
        try:
            dograh_client = get_dograh_client()
        except DograhConfigurationError:
            logger.error("dograh_not_configured_reconciliation_disabled")

    logger.info("worker_started", extra={"worker_id": consumer_name})

    iteration = 0
    errors = 0
    last_reclaim = last_reconcile = time.monotonic()
    try:
        while not _shutdown_requested:
            iteration += 1
            db = SessionLocal()
            try:
                now = time.monotonic()
                if now - last_reclaim >= _RECLAIM_EVERY_SECONDS:
                    last_reclaim = now
                    reclaimed = queue.reclaim_stale(consumer_name, settings.queue_reclaim_idle_ms)
                    if reclaimed:
                        metrics.incr(metrics.STALE_JOBS, len(reclaimed))
                        logger.info(
                            "reclaimed_stale_jobs",
                            extra={"count": len(reclaimed), "worker_id": consumer_name},
                        )
                        process_reclaimed(
                            db, queue, admission, provider, circuit_breaker, reclaimed
                        )

                if iteration % _RECOVERY_DISPATCH_EVERY_N_ITERATIONS == 0:
                    dispatched = dispatch_due_recovery_jobs(recovery_scheduler, queue)
                    if dispatched:
                        logger.info("recovery_jobs_dispatched", extra={"count": dispatched})

                if dograh_client is not None and now - last_reconcile >= _RECONCILE_EVERY_SECONDS:
                    last_reconcile = now
                    handled = reconcile_stale_attempts(db, dograh_client)
                    handled += readmit_missing_analysis(db)
                    if handled:
                        logger.info("reconciler_handled", extra={"count": handled})

                outcome = process_one_job(
                    db, queue, admission, provider, circuit_breaker, consumer_name=consumer_name
                )
                db.commit()
                errors = 0
                if outcome != JobOutcome.NO_JOB:
                    logger.info("job_processed", extra={"outcome": outcome})
                _write_heartbeat()
            except Exception:
                db.rollback()
                errors += 1
                metrics.incr(metrics.WORKER_FAILURES)
                logger.exception("job_processing_error", extra={"consecutive_errors": errors})
                # Redis/PostgreSQL outage: back off exponentially instead of
                # spinning. Nothing is dialed while a dependency is down.
                _sleep_interruptibly(min(2.0**errors, _MAX_ERROR_BACKOFF_SECONDS))
            finally:
                db.close()
    finally:
        logger.info("worker_shutting_down", extra={"worker_id": consumer_name})
        try:
            get_redis().close()
        finally:
            engine.dispose()


if __name__ == "__main__":
    run()
