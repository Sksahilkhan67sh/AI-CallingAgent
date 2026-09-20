"""Working memory persistence -- Checkpoint 04 Steps 11-13, 34."""

from app.models.enums import ConversationPhase
from app.services.ai.memory.schema import ScriptProgress, WorkingMemory
from app.services.ai.memory.store import MemoryStore
from tests.ai_helpers import create_connected_call


def test_checkpoint_persists_to_postgres(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0040")
    store = MemoryStore(db_session, redis_client=None)
    memory = WorkingMemory(
        attempt_id=str(attempt.id),
        contact_id=str(contact.id),
        captured_entities={"interest_level": "interested"},
        script_progress=ScriptProgress(phase=ConversationPhase.DISCOVERY),
    )

    store.checkpoint(memory)

    from sqlalchemy import select

    from app.models.working_memory_snapshot import WorkingMemorySnapshot

    row = db_session.execute(
        select(WorkingMemorySnapshot).where(WorkingMemorySnapshot.attempt_id == attempt.id)
    ).scalar_one()
    assert row.captured_entities == {"interest_level": "interested"}
    assert row.script_progress["phase"] == "Discovery"


def test_load_latest_returns_the_most_recent_snapshot(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0041")
    store = MemoryStore(db_session, redis_client=None)

    first = WorkingMemory(attempt_id=str(attempt.id), contact_id=str(contact.id))
    first.last_agent_utterance = "first"
    store.checkpoint(first)

    second = WorkingMemory(attempt_id=str(attempt.id), contact_id=str(contact.id))
    second.last_agent_utterance = "second"
    store.checkpoint(second)

    loaded = store.load_latest(str(attempt.id), str(contact.id))
    assert loaded.last_agent_utterance == "second"


def test_load_latest_returns_none_when_nothing_checkpointed(db_session):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0042")
    store = MemoryStore(db_session, redis_client=None)

    assert store.load_latest(str(attempt.id), str(contact.id)) is None


def test_memory_is_not_the_transcript(db_session):
    """Step 12: the snapshot must not contain a `turns`/transcript field."""
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0043")
    memory = WorkingMemory(attempt_id=str(attempt.id), contact_id=str(contact.id))

    snapshot = memory.to_snapshot_dict()

    assert "turns" not in snapshot
    assert "transcript" not in snapshot


def test_redis_cache_used_when_available(db_session, redis_client):
    _, contact, attempt = create_connected_call(db_session, phone="555-950-0044")
    store = MemoryStore(db_session, redis_client=redis_client)
    memory = WorkingMemory(attempt_id=str(attempt.id), contact_id=str(contact.id))
    memory.last_agent_utterance = "cached value"

    store.checkpoint(memory)
    cached = redis_client.get(f"conversation_memory:{attempt.id}")

    assert cached is not None


def test_falls_back_to_postgres_when_redis_unavailable(db_session):
    """Step 34: if Redis disappears, memory must still be reconstructable
    from PostgreSQL -- simulated with a redis client pointed at a
    non-existent server."""
    import redis as redis_lib

    _, contact, attempt = create_connected_call(db_session, phone="555-950-0045")
    broken_redis = redis_lib.Redis(host="localhost", port=1, socket_connect_timeout=1)
    store = MemoryStore(db_session, redis_client=broken_redis)

    memory = WorkingMemory(attempt_id=str(attempt.id), contact_id=str(contact.id))
    memory.last_agent_utterance = "durable value"
    store.checkpoint(memory)  # must not raise despite Redis being unreachable

    loaded = store.load_latest(str(attempt.id), str(contact.id))
    assert loaded.last_agent_utterance == "durable value"  # recovered from Postgres
