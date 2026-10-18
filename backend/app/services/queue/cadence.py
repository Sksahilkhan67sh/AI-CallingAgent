"""Time-based, jittered cadence for reclaiming a dead worker's pending jobs (P1-6).

The worker used to reclaim every 50 LOOP ITERATIONS. An iteration is one blocking read (about a
second when idle) or one whole dial (seconds when busy), so the real interval swung with load: a
busy worker reclaimed after minutes. This is wall-clock time, checked on every iteration however
long the previous one took, with +/- jitter so a fleet of workers does not all hit Redis at once.
"""

import random
import time
from collections.abc import Callable


class ReclaimClock:
    def __init__(
        self,
        interval_seconds: float,
        jitter_ratio: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._interval = interval_seconds
        self._jitter = jitter_ratio
        self._clock = clock
        self._rng = rng
        # Due immediately: a worker that has just started should pick up what a crashed
        # predecessor left behind without waiting a full interval.
        self._next = clock()

    def _next_interval(self) -> float:
        return self._interval * (1.0 + self._jitter * (2.0 * self._rng() - 1.0))

    def due(self) -> bool:
        """True at most once per (jittered) interval, however often it is asked."""
        now = self._clock()
        if now < self._next:
            return False
        self._next = now + self._next_interval()
        return True
