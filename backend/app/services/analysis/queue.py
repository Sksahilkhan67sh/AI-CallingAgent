"""Redis Streams-backed durable analysis queue -- Checkpoint 06 §7-8.

A dedicated stream + consumer group, separate from CP03's
calls:outbound (per §7: "if the architecture supports separate Redis
Streams for worker pools, create a dedicated analysis stream/consumer
group"). Same at-least-once delivery / crash-recovery shape as
app/services/queue/redis_queue.py's RedisStreamQueue -- duplicated
rather than generalized because the two carry different job types and
the existing class isn't generic over payload type; introducing a
generic base for two call sites would be exactly the kind of
speculative abstraction the project conventions warn against.
"""

from typing import Any, cast

import redis

from app.services.analysis.job import AnalysisJob


class AnalysisQueue:
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

    def enqueue(self, job: AnalysisJob) -> str:
        message_id = self.redis.xadd(
            self.stream_key, {"job": job.to_json()}, maxlen=200_000, approximate=True
        )
        return cast(str, message_id)

    def read_one(self, consumer_name: str, block_ms: int) -> tuple[str, AnalysisJob] | None:
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
        return message_id, AnalysisJob.from_json(fields["job"])

    def ack(self, message_id: str) -> None:
        self.redis.xack(self.stream_key, self.group, message_id)

    def reclaim_stale(
        self, consumer_name: str, idle_ms: int, count: int = 10
    ) -> list[tuple[str, AnalysisJob]]:
        """A job left PROCESSING by a crashed worker, or one intentionally
        left unacked after a transient failure to get a natural backoff
        window before redelivery (Checkpoint 06 §21-22) -- both are
        recovered the same way CP03 recovers a crashed dialer worker's
        unacked job."""
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
        _, claimed, _ = pending
        return [(msg_id, AnalysisJob.from_json(fields["job"])) for msg_id, fields in claimed]
