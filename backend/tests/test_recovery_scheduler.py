"""RecoveryScheduler -- Checkpoint 05 §14, §21 (real Redis)."""

from datetime import UTC, datetime, timedelta

from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import RecoveryScheduler


def _job(attempt_number=2):
    import uuid

    return RecoveryJob.new(
        attempt_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        attempt_number=attempt_number,
    )


def test_scheduled_job_is_not_due_before_its_time(redis_client):
    scheduler = RecoveryScheduler(redis_client)
    job = _job()

    scheduler.schedule(job, datetime.now(UTC) + timedelta(hours=1))

    assert scheduler.due_jobs(datetime.now(UTC)) == []
    assert scheduler.pending_count() == 1


def test_scheduled_job_becomes_due_at_its_time(redis_client):
    scheduler = RecoveryScheduler(redis_client)
    job = _job()

    scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))

    due = scheduler.due_jobs(datetime.now(UTC))
    assert len(due) == 1
    assert RecoveryJob.from_json(due[0]).job_id == job.job_id


def test_claim_removes_the_job_and_returns_true_for_the_winner(redis_client):
    scheduler = RecoveryScheduler(redis_client)
    job = _job()
    scheduler.schedule(job, datetime.now(UTC) - timedelta(seconds=1))
    job_json = scheduler.due_jobs(datetime.now(UTC))[0]

    assert scheduler.claim(job_json) is True
    assert scheduler.pending_count() == 0


def test_claim_returns_false_for_the_second_caller():
    """Two workers racing to claim the same due job -- only one wins."""
    import redis as redis_lib

    client = redis_lib.Redis.from_url("redis://localhost:6379/1", decode_responses=True)
    client.flushdb()
    scheduler_a = RecoveryScheduler(client)
    scheduler_b = RecoveryScheduler(client)
    job = _job()
    scheduler_a.schedule(job, datetime.now(UTC) - timedelta(seconds=1))
    job_json = scheduler_a.due_jobs(datetime.now(UTC))[0]

    first = scheduler_a.claim(job_json)
    second = scheduler_b.claim(job_json)

    assert first is True
    assert second is False
    client.flushdb()
    client.close()


def test_reschedule_moves_the_job_forward_without_dropping_it(redis_client):
    scheduler = RecoveryScheduler(redis_client)
    job = _job()
    job_json = job.to_json()

    scheduler.reschedule(job_json, datetime.now(UTC) + timedelta(minutes=1))

    assert scheduler.pending_count() == 1
    assert scheduler.due_jobs(datetime.now(UTC)) == []
