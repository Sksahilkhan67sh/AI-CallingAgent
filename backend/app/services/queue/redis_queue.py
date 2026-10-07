"""Redis Streams-backed durable queue -- Checkpoint 03 Step 2.

At-least-once delivery via XREADGROUP/XACK; crash recovery via
XPENDING/XCLAIM (Steps 4-5, 21-22). See docs/CHECKPOINT-03-NOTES.md for
why one stream + a consumer group rather than one stream per campaign.

Redis-py 5.x's sync client methods are typed to also cover the async
client (returning `Awaitable[Any] | Any`), so several lines below cast
the return value to the type this sync usage actually gets back.
"""

from functools import cached_property
from typing import Any, cast

import redis
from redis.commands.core import Script

from app.services.queue.job import DialJob

# Bounded, approximate trim (Step 39): a resource-safety measure, not a correctness one.
_STREAM_MAXLEN = 200_000

# KEYS: stream, guard. ARGV: guard ttl seconds, stream maxlen, job json. 1 = queued, 0 = the
# guard was already set. One script, so no other client can slip between the check and the add.
# Redis scripts do not roll back: the job is added BEFORE the guard is set, so a failing XADD
# can never leave a guard behind with no job (the worst a failure here can do is leave a job
# without its guard, i.e. a later harmless duplicate reference -- never a lost one).
_ENQUEUE_ONCE_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*', 'job', ARGV[3])
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
return 1
"""


class RedisStreamQueue:
    def __init__(self, redis_client: redis.Redis, stream_key: str, group: str) -> None:
        self.redis = redis_client
        self.stream_key = stream_key
        self.group = group
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream_key, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, job: DialJob) -> str:
        # A bounded, approximate trim keeps the stream from growing
        # without limit (Step 39) -- it's a resource-safety measure, not
        # a correctness one; delivery/ack semantics don't depend on it.
        message_id = self.redis.xadd(
            self.stream_key, {"job": job.to_json()}, maxlen=_STREAM_MAXLEN, approximate=True
        )
        return cast(str, message_id)

    def enqueue_once(self, job: DialJob, guard_key: str, guard_ttl_seconds: int) -> bool:
        """CP12-C: add the job unless its duplicate-enqueue guard exists, and set the guard --
        in ONE atomic Redis step. True = queued; False = already guarded (nothing added).

        Before this the guard was set first and the job added second: a failure in between left
        a guard with no job, so that contact could not be enqueued again for the guard's
        lifetime. Raises redis.RedisError if Redis cannot answer."""
        queued = self._enqueue_once(
            keys=[self.stream_key, guard_key],
            args=[guard_ttl_seconds, _STREAM_MAXLEN, job.to_json()],
        )
        return bool(queued)

    @cached_property
    def _enqueue_once(self) -> Script:
        return self.redis.register_script(_ENQUEUE_ONCE_LUA)

    def read_one(self, consumer_name: str, block_ms: int) -> tuple[str, DialJob] | None:
        """Blocks up to `block_ms` for a new job. Returns (message_id, job)
        or None if nothing arrived in that window."""
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
        return message_id, DialJob.from_json(fields["job"])

    def ack(self, message_id: str) -> None:
        self.redis.xack(self.stream_key, self.group, message_id)

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
        return [(msg_id, DialJob.from_json(fields["job"])) for msg_id, fields in claimed]
