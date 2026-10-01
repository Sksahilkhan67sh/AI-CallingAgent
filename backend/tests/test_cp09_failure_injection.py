"""CP09 -- dialer/queue failure injection. Every test asserts the system
fails SAFELY: in particular, that Dograh is never asked to place a second
call for the same attempt."""

import httpx
import pytest

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.conversation import CallEvent
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    SuppressionSource,
)
from app.models.suppression import Suppression
from app.services.queue import dialer_worker
from app.services.queue.dialer_worker import JobOutcome, process_one_job, process_reclaimed
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.dograh_client import DograhApiError, DograhClient, ProviderErrorKind

from . import _cp09 as h


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    h.dograh_engine(monkeypatch)
    yield
    get_settings.cache_clear()


def _run(db, redis_client, provider, queue, **kw):
    return process_one_job(
        db,
        queue,
        kw.get("admission") or h.make_admission(redis_client),
        provider,
        kw.get("breaker") or h.make_breaker(redis_client),
        consumer_name=kw.get("consumer", "w1"),
    )


def _pending(redis_client) -> int:
    return redis_client.xpending("test:cp09:calls", "test:cp09:workers")["pending"]


def test_dograh_read_timeout_is_ambiguous_and_never_creates_a_second_call(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    job = h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh(
        error=DograhApiError(
            408, "timed out", kind=ProviderErrorKind.TIMEOUT, ambiguous=True
        )
    )
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.ADMITTED_AND_DIALED

    (attempt,) = h.attempts_for(db_session, contact)
    assert attempt.state == CallAttemptState.INITIATED  # not failed, not connected
    assert attempt.provider == "dograh" and attempt.provider_call_id is None
    events = [e.event_type for e in db_session.query(CallEvent).all()]
    assert "AMBIGUOUS_PROVIDER_STATE" in events
    # RecoveryManager has NOT been asked to retry: reconciliation comes first.
    assert get_recovery_scheduler().pending_count() == 0

    # The same job redelivered (e.g. after a crash) must not trigger again.
    queue.enqueue(job)
    assert _run(db_session, redis_client, provider, queue) == JobOutcome.ALREADY_PROCESSED
    assert fake.triggers == 1


def test_dograh_5xx_is_ambiguous_not_retried(db_session, redis_client, provider, monkeypatch):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh(error=DograhApiError(503, "unavailable", ambiguous=True))
    h.install(monkeypatch, fake)

    _run(db_session, redis_client, provider, queue)

    (attempt,) = h.attempts_for(db_session, contact)
    assert attempt.state == CallAttemptState.INITIATED
    assert get_recovery_scheduler().pending_count() == 0
    assert fake.triggers == 1


@pytest.mark.parametrize("status", [400, 401, 404, 422, 429])
def test_dograh_4xx_is_a_definite_failure_handed_to_recovery(
    db_session, redis_client, provider, monkeypatch, status
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh(error=DograhApiError(status, "no"))
    h.install(monkeypatch, fake)

    _run(db_session, redis_client, provider, queue)

    (attempt,) = h.attempts_for(db_session, contact)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    # provider_error is retryable by the canonical policy: RecoveryManager
    # (the only retry owner) scheduled retry #1.
    assert get_recovery_scheduler().pending_count() == 1
    assert fake.triggers == 1


def test_connect_failure_before_send_is_safe_to_retry(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    client = DograhClient(
        base_url="https://dograh.example.com", api_key="k", trigger_uuid="u", mode="production"
    )

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", refuse)
    h.install(monkeypatch, client)

    _run(db_session, redis_client, provider, queue)

    (attempt,) = h.attempts_for(db_session, contact)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert get_recovery_scheduler().pending_count() == 1


def test_crash_after_intent_commit_blocks_any_second_trigger(
    db_session, redis_client, provider, monkeypatch
):
    """Worker died after committing intent but before/while calling Dograh."""
    campaign, contact = h.setup_contact(db_session)
    contact.attempt_count = 1
    db_session.add(
        CallAttempt(
            contact_id=contact.id,
            attempt_number=1,
            state=CallAttemptState.INITIATED,
            provider="dograh",
        )
    )
    db_session.flush()
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.ALREADY_PROCESSED
    assert fake.triggers == 0
    assert _pending(redis_client) == 0  # acked: safely handled


def test_redis_unavailable_during_admission_dials_nothing_and_leaves_job(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)
    admission = h.make_admission(redis_client)

    def boom(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(admission, "try_admit", boom)
    with pytest.raises(ConnectionError):
        _run(db_session, redis_client, provider, queue, admission=admission)

    assert fake.triggers == 0
    assert _pending(redis_client) == 1  # left for reclaim, nothing lost
    assert h.attempts_for(db_session, contact) == []


def test_postgres_failure_before_commit_does_not_ack_the_job(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    h.install(monkeypatch, h.FakeDograh())

    real_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] >= 2:  # the post-dial commit (call 1 is the intent commit)
            raise RuntimeError("postgres transient failure")
        real_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)
    with pytest.raises(RuntimeError):
        _run(db_session, redis_client, provider, queue)

    assert _pending(redis_client) == 1  # NOT acked before durable persistence


def test_suppression_added_while_job_is_queued_prevents_the_call(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    db_session.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opted out while queued",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.NOT_ELIGIBLE
    assert fake.triggers == 0


def test_campaign_paused_while_job_is_queued_prevents_the_call(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    campaign.status = CampaignStatus.PAUSED
    db_session.flush()
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.NOT_ELIGIBLE
    assert fake.triggers == 0


def test_attempt_beyond_the_retry_budget_is_not_dialed(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)  # canonical max_retries=2 -> 3 attempts
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact, attempt_number=4)
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.NOT_ELIGIBLE
    assert fake.triggers == 0


def test_stale_job_from_a_dead_worker_is_reclaimed_and_dialed_exactly_once(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    assert queue.read_one("dead-worker", 10) is not None  # claimed, never acked
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    reclaimed = queue.reclaim_stale("w2", 0)
    assert len(reclaimed) == 1
    handled = process_reclaimed(
        db_session,
        queue,
        h.make_admission(redis_client),
        provider,
        h.make_breaker(redis_client),
        reclaimed,
    )
    assert handled == 1 and fake.triggers == 1
    assert _pending(redis_client) == 0
    assert queue.reclaim_stale("w3", 0) == []  # nothing left to double-process


def test_stale_job_whose_call_was_already_placed_is_not_dialed_again(
    db_session, redis_client, provider, monkeypatch
):
    """Dead worker placed the call and committed, but died before the ack."""
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)
    admission = h.make_admission(redis_client)
    breaker = h.make_breaker(redis_client)
    monkeypatch.setattr(queue, "ack", lambda _id: None)  # worker dies before ack
    assert _run(db_session, redis_client, provider, queue, admission=admission) == (
        JobOutcome.ADMITTED_AND_DIALED
    )
    monkeypatch.undo()
    h.install(monkeypatch, fake)
    h.dograh_engine(monkeypatch)

    reclaimed = queue.reclaim_stale("w2", 0)
    process_reclaimed(db_session, queue, admission, provider, breaker, reclaimed)
    assert fake.triggers == 1
    assert _pending(redis_client) == 0


def test_poison_job_is_dead_lettered_durably_without_dialing(
    db_session, redis_client, provider, monkeypatch
):
    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    job = h.enqueue(queue, campaign, contact)
    monkeypatch.setenv("QUEUE_MAX_DELIVERIES", "2")
    get_settings.cache_clear()
    h.install(monkeypatch, h.FakeDograh())

    def explode(*a, **k):
        raise RuntimeError("corrupt job")

    monkeypatch.setattr(dialer_worker, "_dial", explode)

    with pytest.raises(RuntimeError):
        _run(db_session, redis_client, provider, queue)
    reclaimed = queue.reclaim_stale("w2", 0)
    assert len(reclaimed) == 1
    outcome = dialer_worker._process(
        db_session,
        queue,
        h.make_admission(redis_client),
        provider,
        h.make_breaker(redis_client),
        reclaimed[0][0],
        reclaimed[0][1],
    )

    assert outcome == JobOutcome.DEAD_LETTERED
    assert redis_client.xlen("calls:dlq") == 1
    assert _pending(redis_client) == 0
    row = db_session.query(AuditLog).filter(AuditLog.action == "queue.dead_lettered").one()
    assert row.event_metadata["contact_id"] == str(contact.id)
    assert row.event_metadata["correlation_id"] == job.trace_id
    assert row.event_metadata["reason"] == "max_processing_failures"


def test_dograh_concurrency_is_enforced_from_postgres(
    db_session, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("CAMPAIGN_CONCURRENCY_LIMIT", "1")
    get_settings.cache_clear()
    campaign, first = h.setup_contact(db_session, phone="555-970-0010")
    _, second = h.setup_contact(db_session, phone="555-970-0011", campaign=campaign)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, first)
    h.enqueue(queue, campaign, second)
    fake = h.FakeDograh()
    h.install(monkeypatch, fake)

    assert _run(db_session, redis_client, provider, queue) == JobOutcome.ADMITTED_AND_DIALED
    # first call is still in flight (INITIATED, awaiting completion): no room
    assert _run(db_session, redis_client, provider, queue) == JobOutcome.NOT_ADMITTED
    assert fake.triggers == 1
    assert _pending(redis_client) == 1


def test_provider_outage_does_not_create_a_retry_storm(
    db_session, redis_client, provider, monkeypatch
):
    breaker = h.make_breaker(redis_client)
    threshold = breaker.error_threshold
    campaign, _ = h.setup_contact(db_session, phone="555-970-0020")
    queue = h.make_queue(redis_client)
    contacts = [
        h.setup_contact(db_session, phone=f"555-971-{i:04d}", campaign=campaign)[1]
        for i in range(threshold + 6)
    ]
    for c in contacts:
        h.enqueue(queue, campaign, c)
    fake = h.FakeDograh(error=DograhApiError(503, "down", ambiguous=True))
    h.install(monkeypatch, fake)
    monkeypatch.setattr(dialer_worker, "_ADMISSION_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(dialer_worker, "_ADMISSION_RETRY_SLEEP_SECONDS", 0)

    for _ in contacts:
        _run(db_session, redis_client, provider, queue, breaker=breaker)

    assert breaker.is_open()
    assert fake.triggers == threshold  # the open circuit stopped the rest
    assert get_recovery_scheduler().pending_count() == 0


def test_shutdown_signal_during_execution_still_commits_and_acks(
    db_session, redis_client, provider, monkeypatch
):
    from app import worker

    campaign, contact = h.setup_contact(db_session)
    queue = h.make_queue(redis_client)
    h.enqueue(queue, campaign, contact)
    fake = h.FakeDograh(on_trigger=lambda: worker._handle_shutdown_signal(15, None))
    h.install(monkeypatch, fake)
    worker._shutdown_requested = False
    try:
        outcome = _run(db_session, redis_client, provider, queue)
        assert worker._shutdown_requested is True
    finally:
        worker._shutdown_requested = False

    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    (attempt,) = h.attempts_for(db_session, contact)
    assert attempt.provider_call_id == "4242"
    assert _pending(redis_client) == 0  # in-flight job finished cleanly
    assert contact.status == ContactStatus.DIALING
