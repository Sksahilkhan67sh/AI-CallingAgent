"""Per-job deadline (P1-6).

One hung call (a database lock, a stuck socket, a provider that never answers) must not hold the
only worker, or a pending stream entry, for ever. `job_deadline(seconds)` raises
`JobDeadlineExceeded` inside whatever blocking call is running once the time is up, using SIGALRM,
which interrupts blocking syscalls. It works only in the main thread of a POSIX process -- which is
where the worker loop runs -- and is a harmless no-op anywhere else.

An interruption looks, to the database and to Dograh, exactly like the worker being killed at that
instant, a case the claim/commit protocol, the reconcile path and the stuck-call sweeper already
handle without ever dialing twice. The pending entry is left unacked; reclaim re-drives it.
"""

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType


class JobDeadlineExceeded(Exception):
    """The job ran longer than JOB_DEADLINE_SECONDS and was abandoned."""


def _supported() -> bool:
    return (
        hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )


@contextmanager
def job_deadline(seconds: float) -> Iterator[None]:
    if seconds <= 0 or not _supported():
        yield
        return

    def _expired(signum: int, frame: FrameType | None) -> None:
        raise JobDeadlineExceeded(f"job exceeded its {seconds:g}s deadline")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
