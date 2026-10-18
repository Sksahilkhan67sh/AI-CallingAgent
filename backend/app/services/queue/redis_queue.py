"""Redis Streams-backed durable queue -- Checkpoint 03 Step 2.

At-least-once delivery via XREADGROUP/XACK; crash recovery via
XPENDING/XCLAIM (Steps 4-5, 21-22). See docs/CHECKPOINT-03-NOTES.md for
why one stream + a consumer group rather than one stream per campaign.

Redis-py 5.x's sync client methods are typed to also cover the async
client (returning `Awaitable[Any] | Any`), so several lines below cast
the return value to the type this sync usage actually gets back.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any, cast

import redis
from redis.commands.core import Script

from app.core.config import get_settings
from app.services.queue.job import DialJob

logger = logging.getLogger("queue")

REASON_MALFORMED = "malformed_payload"
REASON_MAX_DELIVERIES = "max_deliveries_exceeded"
# XCLAIM needs a consumer to hand the entry to; it is only used to reset a delivery counter.
_HOLD_CONSUMER = "hold-reset"


@dataclass(frozen=True)
class DeadLetter:
    """A message moved to the dead-letter stream. `job` is None when the payload was unreadable."""

    message_id: str
    reason: str
    raw: str
    times_delivered: int
    job: DialJob | None


class QueueBacklogFull(Exception):
    """The stream already holds `queue_max_backlog` jobs. Nothing was added and no guard was
    set: the caller leaves the contact PENDING in PostgreSQL (or reschedules it) and tries
    again later. A job is never dropped to make room (P1-1)."""


# KEYS: stream, guard. ARGV: guard ttl seconds, max backlog, job json. 1 = queued, 0 = the
# guard was already set, -1 = the backlog is full. One script, so no other client can slip
# between the check and the add. The stream is never trimmed by length: `XADD MAXLEN ~` used to
# evict the OLDEST entries even when no consumer had read them (P1-1).
# Redis scripts do not roll back: the job is added BEFORE the guard is set, so a failing XADD
# can never leave a guard behind with no job (the worst a failure here can do is leave a job
# without its guard, i.e. a later harmless duplicate reference -- never a lost one).
_ENQUEUE_ONCE_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[2]) then
  return -1
end
redis.call('XADD', KEYS[1], '*', 'job', ARGV[3])
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
return 1
"""


class RedisStreamQueue:
    def __init__(self, redis_client: redis.Redis, stream_key: str, group: str) -> None:
        self.redis = redis_client
        self.stream_key = stream_key
        self.group = group
        self.dlq_key = f"{stream_key}:dlq"
        self._dead_letters: list[DeadLetter] = []
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream_key, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, job: DialJob) -> str:
        # Never refused and never trimmed: recovery retries use this and must not be dropped.
        # Entries are released only by ack(); first-attempt backpressure is in enqueue_once.
        message_id = self.redis.xadd(self.stream_key, {"job": job.to_json()})
        return cast(str, message_id)

    def enqueue_once(self, job: DialJob, guard_key: str, guard_ttl_seconds: int) -> bool:
        """CP12-C: add the job unless its duplicate-enqueue guard exists, and set the guard --
        in ONE atomic Redis step. True = queued; False = already guarded (nothing added).
        Raises QueueBacklogFull when the stream holds `queue_max_backlog` jobs (nothing added).

        Before this the guard was set first and the job added second: a failure in between left
        a guard with no job, so that contact could not be enqueued again for the guard's
        lifetime. Raises redis.RedisError if Redis cannot answer."""
        queued = self._enqueue_once(
            keys=[self.stream_key, guard_key],
            args=[guard_ttl_seconds, get_settings().queue_max_backlog, job.to_json()],
        )
        if queued == -1:
            raise QueueBacklogFull(self.stream_key)
        return bool(queued)

    @cached_property
    def _enqueue_once(self) -> Script:
        return self.redis.register_script(_ENQUEUE_ONCE_LUA)

    def read_one(self, consumer_name: str, block_ms: int) -> tuple[str, DialJob] | None:
        """Blocks up to `block_ms` for a new job. Returns (message_id, job)
        or None if nothing arrived in that window. An unreadable message is moved to the
        dead-letter stream and the read carries on: it can never block the jobs behind it."""
        while True:
            response = cast(
                Any,
                self.redis.xreadgroup(
                    self.group, consumer_name, {self.stream_key: ">"}, count=1, block=block_ms
                ),
            )
            if not response:
                return None
            _, messages = response[0]
            message_id, fields = messages[0]
            job = _parse(fields)
            if job is not None:
                return message_id, job
            self._dead_letter(message_id, fields, REASON_MALFORMED, times_delivered=1, job=None)

    def ack(self, message_id: str) -> None:
        # XACK then XDEL in one transaction: an entry is removed only once it is acknowledged,
        # so the stream holds exactly the unread plus in-flight jobs and cannot grow forever.
        pipe = self.redis.pipeline(transaction=True)
        pipe.xack(self.stream_key, self.group, message_id)
        pipe.xdel(self.stream_key, message_id)
        pipe.execute()

    def reclaim_stale(
        self, consumer_name: str, idle_ms: int, count: int = 10
    ) -> list[tuple[str, DialJob]]:
        """Step 21-22: claim messages that another consumer read but
        never acked within `idle_ms` -- a crashed worker's leftover
        work. Safe to call from any worker; XCLAIM is atomic, so two
        workers racing to reclaim the same stale message only one wins.
        """
        pending = cast(
            Any,
            self.redis.xautoclaim(
                self.stream_key,
                self.group,
                consumer_name,
                min_idle_time=idle_ms,
                start_id="0",
                count=count,
            ),
        )
        # xautoclaim returns (next_cursor, claimed_messages, deleted_ids)
        _, claimed, _ = pending
        max_deliveries = get_settings().queue_max_deliveries
        ready: list[tuple[str, DialJob]] = []
        for msg_id, fields in claimed:
            job = _parse(fields)
            delivered = self._times_delivered(msg_id)
            if job is None:
                self._dead_letter(msg_id, fields, REASON_MALFORMED, delivered, None)
            elif delivered > max_deliveries:
                # Delivered this many times and never settled or deliberately held: poison.
                self._dead_letter(msg_id, fields, REASON_MAX_DELIVERIES, delivered, job)
            else:
                ready.append((msg_id, job))
        return ready

    def _times_delivered(self, message_id: str) -> int:
        rows = cast(
            Any,
            self.redis.xpending_range(
                self.stream_key, self.group, min=message_id, max=message_id, count=1
            ),
        )
        return int(rows[0]["times_delivered"]) if rows else 1

    def reset_deliveries(self, message_id: str) -> None:
        """A job deliberately left unacked (campaign paused, outbound frozen, window closed, call
        in flight) is re-driven by reclaim, and every reclaim counts as a delivery. Reset the
        counter so a legitimate hold is never mistaken for poison. XCLAIM RETRYCOUNT with JUSTID
        sets the counter without incrementing it."""
        try:
            self.redis.xclaim(
                self.stream_key,
                self.group,
                _HOLD_CONSUMER,
                min_idle_time=0,
                message_ids=[message_id],
                retrycount=0,
                justid=True,
            )
        except redis.RedisError:
            # Best effort: Redis may have been flushed (no stream, no group) or be unreachable.
            # The hold outcome itself is unaffected; at worst a very long hold is dead-lettered
            # (preserved in the DLQ with its payload), never lost.
            logger.warning("delivery_reset_failed", extra={"message_id": message_id})

    def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        reason: str,
        times_delivered: int,
        job: DialJob | None,
    ) -> None:
        """Atomically: copy to the DLQ with its reason and original payload, ack and delete the
        original. After this the message can never be delivered again."""
        raw = fields.get("job", "") if isinstance(fields, dict) else ""
        pipe = self.redis.pipeline(transaction=True)
        pipe.xadd(
            self.dlq_key,
            {
                "job": raw,
                "reason": reason,
                "original_id": message_id,
                "stream": self.stream_key,
                "times_delivered": str(times_delivered),
                "dead_at": datetime.now(UTC).isoformat(),
            },
        )
        pipe.xack(self.stream_key, self.group, message_id)
        pipe.xdel(self.stream_key, message_id)
        pipe.execute()
        logger.error(
            "job_dead_lettered",
            extra={"message_id": message_id, "reason": reason, "times_delivered": times_delivered},
        )
        self._dead_letters.append(DeadLetter(message_id, reason, raw, times_delivered, job))

    def drain_dead_letters(self) -> list[DeadLetter]:
        """Dead letters created since the last call (for the worker to record in PostgreSQL)."""
        letters, self._dead_letters = self._dead_letters, []
        return letters


def _parse(fields: Any) -> DialJob | None:
    try:
        return DialJob.from_json(fields["job"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
