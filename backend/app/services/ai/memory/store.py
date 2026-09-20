"""Working memory persistence -- Checkpoint 04 Steps 13, 34.

PostgreSQL (`working_memory_snapshot`) is the durable source of truth;
Redis, when available, is only a hot-path read cache in front of it.
If Redis is unavailable, reads/writes silently fall back to Postgres
alone -- the conversation must not become permanently corrupted just
because the cache is down.
"""

import json
import logging
from typing import cast

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.working_memory_snapshot import WorkingMemorySnapshot
from app.services.ai.memory.schema import ObjectionRecord, ScriptProgress, WorkingMemory

logger = logging.getLogger("ai.memory")

_REDIS_TTL_SECONDS = 3600  # hot cache only -- Postgres has the durable copy


class MemoryStore:
    def __init__(self, db: Session, redis_client: redis.Redis | None = None) -> None:
        self.db = db
        self.redis = redis_client

    def checkpoint(self, memory: WorkingMemory) -> None:
        """Step 13: write a durable snapshot after every meaningful turn.
        This is a single INSERT, not a synchronous write per audio
        packet (Step 33)."""
        self.db.add(
            WorkingMemorySnapshot(
                attempt_id=memory.attempt_id,
                **memory.to_snapshot_dict(),
            )
        )
        self.db.flush()

        if self.redis is not None:
            try:
                self.redis.set(
                    self._cache_key(memory.attempt_id),
                    json.dumps(memory.to_snapshot_dict()),
                    ex=_REDIS_TTL_SECONDS,
                )
            except redis.RedisError:
                logger.warning("memory_cache_write_failed", exc_info=True)

    def load_latest(self, attempt_id: str, contact_id: str) -> WorkingMemory | None:
        if self.redis is not None:
            try:
                cached = self.redis.get(self._cache_key(attempt_id))
                if cached:
                    data = json.loads(cast(str, cached))
                    return WorkingMemory(
                        attempt_id=attempt_id,
                        contact_id=contact_id,
                        captured_entities=data["captured_entities"],
                        script_progress=ScriptProgress.from_dict(data["script_progress"]),
                        objections_raised=[
                            ObjectionRecord.from_dict(o) for o in data["objections_raised"]
                        ],
                        last_agent_utterance=data["last_agent_utterance"],
                        disconnect_count=data["disconnect_count"],
                        requires_suppression=data["requires_suppression"],
                        recording_consent=data["recording_consent"],
                        schema_version=data["schema_version"],
                    )
            except (redis.RedisError, KeyError, json.JSONDecodeError):
                logger.warning("memory_cache_read_failed", exc_info=True)
                # fall through to Postgres

        row = self.db.execute(
            select(WorkingMemorySnapshot)
            .where(WorkingMemorySnapshot.attempt_id == attempt_id)
            .order_by(WorkingMemorySnapshot.snapshotted_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            return None
        return WorkingMemory.from_snapshot_row(attempt_id, contact_id, row)

    @staticmethod
    def _cache_key(attempt_id: str) -> str:
        return f"conversation_memory:{attempt_id}"
