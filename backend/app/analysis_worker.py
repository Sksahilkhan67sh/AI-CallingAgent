"""Post-call analysis worker entrypoint -- Checkpoint 06 §7, §22, §35.

    loop { consume -> claim (Postgres-authoritative) -> load transcript
    -> LLM analysis -> validate -> score -> persist -> ack }
    -> graceful shutdown

A separate process/pool from app.worker (the calling/dialer worker):
CP06 §35 requires horizontal analysis workers, and this is a distinct
job type/queue, not a variant of the dialer's. Run with:

    python -m app.analysis_worker

Worker count is horizontal (run more processes), matching the existing
convention -- app.worker is scaled the same way, and neither creates
one worker per job.
"""

import logging
import signal
import socket
import uuid
from types import FrameType

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.llm.factory import get_analysis_llm_provider
from app.services.analysis.worker import (
    AnalysisJobOutcome,
    process_claimed_job,
    process_one_analysis_job,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analysis_worker")

_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("shutdown_requested", extra={"signal": signum})
    _shutdown_requested = True


def run() -> None:
    """Graceful shutdown: stops claiming new jobs on SIGTERM/SIGINT, but
    a job already in flight is allowed to finish so it isn't left
    partially processed and unacked (same contract as app.worker.run)."""
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    consumer_name = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    queue = get_analysis_queue()
    llm = get_analysis_llm_provider()

    logger.info("analysis_worker_started", extra={"consumer_name": consumer_name})

    reclaim_counter = 0
    try:
        while not _shutdown_requested:
            db = SessionLocal()
            try:
                # Periodically reclaim jobs left stale by a crashed
                # worker, or left unacked deliberately for the
                # retry-backoff window (§21-22) -- not on every
                # iteration, same cadence convention as app.worker.
                reclaim_counter += 1
                if reclaim_counter % 50 == 0:
                    reclaimed = queue.reclaim_stale(
                        consumer_name, get_settings().analysis_reclaim_idle_ms
                    )
                    for message_id, reclaimed_job in reclaimed:
                        outcome = process_claimed_job(
                            db, queue, llm, message_id, reclaimed_job
                        )
                        db.commit()
                        logger.info(
                            "reclaimed_analysis_job_processed", extra={"outcome": outcome}
                        )

                outcome = process_one_analysis_job(db, queue, llm, consumer_name=consumer_name)
                db.commit()
                if outcome != AnalysisJobOutcome.NO_JOB:
                    logger.info("analysis_job_processed", extra={"outcome": outcome})
            except Exception:
                db.rollback()
                logger.exception("analysis_job_processing_error")
            finally:
                db.close()
    finally:
        logger.info("analysis_worker_shutting_down", extra={"consumer_name": consumer_name})
        get_redis().close()


if __name__ == "__main__":
    run()
