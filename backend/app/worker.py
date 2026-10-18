"""Calling worker entrypoint -- Checkpoint 03 Step 22.

    connect dependencies -> loop { consume -> claim -> admission ->
    dial -> persist -> ack } -> graceful shutdown

Run with: python -m app.worker
"""

import logging
import random
import signal
import socket
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis
from app.services.admin.system_service import DIALER_HEARTBEAT_KEY
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.cadence import ReclaimClock
from app.services.queue.dialer_worker import JobOutcome, process_one_job, reclaim_and_redrive
from app.services.queue.factory import get_admission_controller, get_queue
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.scheduler import RecoveryScheduler
from app.services.telephony.base import TelephonyProvider
from app.services.telephony.circuit_breaker import CircuitBreaker
from app.services.telephony.factory import get_telephony_provider

logging.basicConfig(level=logging.INFO)
# CP10: httpx's INFO request log would print Dograh's trigger URL (it embeds the trigger UUID).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("worker")

_shutdown_requested = False

# Checkpoint 05: how often (in loop iterations) to check for recovery
# jobs that have come due -- not on every iteration, same reasoning as
# the existing stale-job-reclaim cadence below.
_RECOVERY_DISPATCH_EVERY_N_ITERATIONS = 10


def _rebuild_retry_schedule() -> None:
    """P1-2: restore any retry missing from the Redis cache from PostgreSQL. Never fatal."""
    try:
        from app.services.recovery.rebuild import rebuild_retry_schedule

        restored = rebuild_retry_schedule(SessionLocal, get_recovery_scheduler())
        if restored:
            logger.warning("retry_schedule_rebuilt", extra={"restored": restored})
    except Exception:
        logger.exception("retry_schedule_rebuild_failed")


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


class WorkerLoop:
    """One worker's loop, one `step()` per iteration (P1-6: a class so the cadence is testable
    with an injected clock). One job at a time; `step()` never raises."""

    def __init__(
        self,
        *,
        queue: RedisStreamQueue,
        admission: AdmissionController,
        provider: TelephonyProvider,
        circuit_breaker: CircuitBreaker,
        recovery_scheduler: RecoveryScheduler,
        consumer_name: str,
        session_factory: Callable[[], Session] = SessionLocal,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        block_ms: int = 1000,
        heartbeat: Callable[[], None] = _write_heartbeat,
    ) -> None:
        settings = get_settings()
        self._queue = queue
        self._admission = admission
        self._provider = provider
        self._breaker = circuit_breaker
        self._scheduler = recovery_scheduler
        self._consumer = consumer_name
        self._session_factory = session_factory
        self._clock = clock
        self._block_ms = block_ms
        self._heartbeat = heartbeat
        self._iteration = 0
        # Reclaim is wall-clock time (jittered), not "every N iterations": an iteration can be a
        # whole dial, so the old cadence stretched to minutes on a busy worker.
        self._reclaim = ReclaimClock(
            settings.queue_reclaim_interval_seconds,
            settings.queue_reclaim_jitter_ratio,
            clock=clock,
            rng=rng,
        )
        _rebuild_retry_schedule()  # at start: a Redis that came back empty is repaired first
        self._last_rebuild = clock()

    def step(self) -> str | None:
        db = self._session_factory()
        try:
            settings = get_settings()
            if self._clock() - self._last_rebuild >= settings.retry_rebuild_interval_seconds:
                self._last_rebuild = self._clock()
                _rebuild_retry_schedule()

            if self._reclaim.due():
                # Re-drive jobs a crashed worker left pending. Each job is isolated, poison jobs
                # end in the dead-letter stream, and a deliberate hold is not a failure
                # (reclaim_and_redrive / process_claimed_job).
                reclaim_and_redrive(
                    db,
                    self._queue,
                    self._admission,
                    self._provider,
                    self._breaker,
                    self._consumer,
                )

            self._iteration += 1
            if self._iteration % _RECOVERY_DISPATCH_EVERY_N_ITERATIONS == 0:
                dispatched = dispatch_due_recovery_jobs(self._scheduler, self._queue)
                if dispatched:
                    logger.info("recovery_jobs_dispatched", extra={"count": dispatched})

            outcome = process_one_job(
                db,
                self._queue,
                self._admission,
                self._provider,
                self._breaker,
                consumer_name=self._consumer,
                block_ms=self._block_ms,
            )
            db.commit()
            # outbound_blocked at this level is the idle poll during a kill switch
            # freeze (rate-limited warning in dialer_worker) -- not worth a line a second.
            if outcome not in (
                JobOutcome.NO_JOB,
                JobOutcome.OUTBOUND_BLOCKED,
                JobOutcome.BUDGET_BLOCKED,
            ):
                logger.info("job_processed", extra={"outcome": outcome})

            # Checkpoint 07 §25: best-effort liveness signal for the admin dashboard's system
            # page. See app/services/admin/system_service.py.
            self._heartbeat()
            return outcome
        except Exception:
            db.rollback()
            logger.exception("job_processing_error")
            return None
        finally:
            db.close()


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
    provider = get_telephony_provider()
    loop = WorkerLoop(
        queue=get_queue(),
        admission=get_admission_controller(),
        provider=provider,
        circuit_breaker=CircuitBreaker(get_redis(), provider.name),
        recovery_scheduler=get_recovery_scheduler(),
        consumer_name=consumer_name,
    )

    logger.info("worker_started", extra={"consumer_name": consumer_name})
    try:
        while not _shutdown_requested:
            loop.step()
    finally:
        logger.info("worker_shutting_down", extra={"consumer_name": consumer_name})
        get_redis().close()


if __name__ == "__main__":
    run()
