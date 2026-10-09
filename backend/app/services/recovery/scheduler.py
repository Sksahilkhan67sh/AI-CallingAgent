"""Durable delayed scheduling -- Checkpoint 05 §14, §21.

A Redis sorted set, not asyncio.sleep()/timers/in-memory lists (all
explicitly ruled out) and not a second queue -- see
docs/CHECKPOINT-05-NOTES.md for why a sorted set is the right primitive
here and how it hands off to the *existing* calls:outbound stream once
a job is due.
"""

import json
from dataclasses import asdict
from datetime import datetime
from typing import cast

import redis

from app.services.queue.job import DialJob
from app.services.recovery.job import RecoveryJob

# Marks a scheduled entry that is a first-attempt DialJob deferred to a later calling window
# (CP14), as opposed to a RecoveryJob retry. Existing entries have no "kind" and are retries.
DIAL_JOB_KIND = "dial_job"

_SCHEDULE_KEY = "recovery:scheduled"


class RecoveryScheduler:
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def schedule(self, job: RecoveryJob, due_at: datetime) -> None:
        self.redis.zadd(_SCHEDULE_KEY, {job.to_json(): due_at.timestamp()})

    def schedule_dial(self, job: DialJob, due_at: datetime) -> None:
        """Defer a first-attempt dial to `due_at` (the next calling-window opening). The
        member is canonical JSON, so deferring the same job twice is one entry, not two."""
        member = json.dumps({"kind": DIAL_JOB_KIND, **asdict(job)}, sort_keys=True)
        self.redis.zadd(_SCHEDULE_KEY, {member: due_at.timestamp()})

    def reschedule(self, job_json: str, due_at: datetime) -> None:
        """Used by the dispatcher when a due job is re-checked and found
        not-yet-dialable (paused campaign, closed calling window) --
        moves it forward, never drops it."""
        self.redis.zadd(_SCHEDULE_KEY, {job_json: due_at.timestamp()})

    def due_jobs(self, now: datetime, limit: int = 50) -> list[str]:
        """Candidates whose due time has passed. Does not claim them --
        see `claim`."""
        return cast(
            "list[str]",
            self.redis.zrangebyscore(
                _SCHEDULE_KEY, min="-inf", max=now.timestamp(), start=0, num=limit
            ),
        )

    def claim(self, job_json: str) -> bool:
        """Atomically remove one scheduled entry. ZREM's return value is
        the claim: 1 means this caller removed it (and therefore owns
        processing it); 0 means another worker already claimed/removed
        it first. The same "whoever wins the race owns the job" pattern
        as CP03's queue claiming."""
        removed: int = self.redis.zrem(_SCHEDULE_KEY, job_json)  # type: ignore[assignment]
        return removed == 1

    def pending_count(self) -> int:
        return self.redis.zcard(_SCHEDULE_KEY)  # type: ignore[return-value]
