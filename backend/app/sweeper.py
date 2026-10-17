"""Stuck-call sweeper process: `python -m app.sweeper` (loop) or `python -m app.sweeper --once`.

See app/services/queue/stuck_call_sweeper.py for the policy. Safe to run any number of copies.
"""

import logging
import signal
import sys
import time
from types import FrameType

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.queue.stuck_call_sweeper import sweep_stuck_attempts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sweeper")

_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("sweeper_shutdown_requested", extra={"signal": signum})
    _shutdown_requested = True


def sweep_until_clear() -> int:
    """Run pages until nothing is left to action. Returns the total actioned."""
    total = 0
    while not _shutdown_requested:
        result = sweep_stuck_attempts(SessionLocal)
        total += result.actioned
        if result.actioned == 0 or result.blocked:
            break
    return total


def run(once: bool = False) -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    interval = get_settings().sweeper_interval_seconds
    logger.info("sweeper_started", extra={"once": once})
    while not _shutdown_requested:
        try:
            sweep_until_clear()
        except Exception:
            logger.exception("sweeper_error")  # never exits on a bad pass; next pass retries
        if once:
            return
        deadline = time.monotonic() + interval
        while not _shutdown_requested and time.monotonic() < deadline:
            time.sleep(1)
    logger.info("sweeper_stopped")


if __name__ == "__main__":
    run(once="--once" in sys.argv[1:])
