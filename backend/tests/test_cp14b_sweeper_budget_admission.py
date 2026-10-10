"""CP14B -- pending-analysis sweeper, daily estimated-spend ledger, post-commit admission.

Real PostgreSQL + Redis. Real OS threads (separate connections, genuinely committed rows) are
used where a race is the point; those tests clean up after themselves.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from app.core.config import get_settings
from app.models.call_analysis import AnalysisBudgetDay, CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.services.analysis import budget
from app.services.analysis.admission import enqueue_call_analysis
from app.services.analysis.budget import BudgetDecision
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.repository import CallAnalysisRepository
from app.services.analysis.sweeper import sweep
from tests.phone_helpers import normalize_phone_number
from tests.test_call_analysis_worker import _admitted

NOW = datetime.now(UTC)


def _after_window() -> datetime:
    """A moment safely past the republish window, measured from the REAL clock at call time
    (rows carry real admission timestamps, so a module-level constant would drift)."""
    return datetime.now(UTC) + timedelta(
        seconds=get_settings().analysis_republish_after_seconds + 1
    )


def _row(db, attempt):
    db.expire_all()
    return db.query(CallAnalysis).filter_by(call_attempt_id=attempt.id).one()


def _jobs(redis_client):
    queue = get_analysis_queue()
    return redis_client.xlen(queue.stream_key)


# ------------------------------------------------------------------ sweeper


def test_sweeper_republishes_a_pending_analysis_lost_from_redis(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0001")
    assert _jobs(redis_client) == 1
    redis_client.flushdb()  # Redis restart / data loss: the only queue entry is gone
    assert _jobs(redis_client) == 0

    # Freshly admitted work is not republished immediately (it is presumed in flight) ...
    assert sweep(db_session, get_analysis_queue(), now=NOW).published == 0
    # ... but once the republish window has passed it is rediscovered from PostgreSQL.
    later = _after_window()
    report = sweep(db_session, get_analysis_queue(), now=later)
    assert report.published == 1 and _jobs(redis_client) == 1
    assert _row(db_session, attempt).last_enqueued_at == later


def test_repeated_sweeps_are_idempotent_and_publish_once_per_window(db_session, redis_client):
    _admitted(db_session, phone="989-500-0002")
    redis_client.flushdb()
    later = _after_window()
    queue = get_analysis_queue()
    first, second, third = (sweep(db_session, queue, now=later).published for _ in range(3))
    assert (first, second, third) == (1, 0, 0)
    assert _jobs(redis_client) == 1


def test_retry_wait_is_published_only_once_it_is_due(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0003")
    row = _row(db_session, attempt)
    row.status = AnalysisStatus.RETRY_WAIT
    row.next_attempt_at = NOW + timedelta(minutes=10)
    row.last_enqueued_at = NOW
    db_session.commit()
    redis_client.flushdb()
    queue = get_analysis_queue()

    assert sweep(db_session, queue, now=NOW + timedelta(minutes=5)).published == 0  # not due
    assert sweep(db_session, queue, now=NOW + timedelta(minutes=11)).published == 1  # due
    assert sweep(db_session, queue, now=NOW + timedelta(minutes=12)).published == 0  # once only


def test_expired_processing_lease_is_republished_and_a_live_one_is_not(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0004")
    repo = CallAnalysisRepository(db_session)
    repo.claim_for_processing(
        _row(db_session, attempt).id, now=NOW, lease_seconds=120, max_attempts=3
    )
    db_session.commit()
    redis_client.flushdb()
    queue = get_analysis_queue()

    assert sweep(db_session, queue, now=NOW + timedelta(seconds=60)).published == 0  # lease live
    assert sweep(db_session, queue, now=NOW + timedelta(seconds=130)).published == 1  # expired


def test_sweeper_never_resets_or_republishes_terminal_failures(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0005")
    row = _row(db_session, attempt)
    row.status, row.error_code = AnalysisStatus.FAILED, "provider_permanent_error"
    row.last_enqueued_at = None
    db_session.commit()
    redis_client.flushdb()
    far = NOW + timedelta(days=30)
    assert sweep(db_session, get_analysis_queue(), now=far).published == 0
    assert _row(db_session, attempt).status == AnalysisStatus.FAILED
    assert _jobs(redis_client) == 0


def test_sweeper_registers_a_completed_call_that_has_no_analysis_row(db_session, redis_client):
    campaign = Campaign(name="cp14b sweeper")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="989-500-0006",
        normalized_phone_number=normalize_phone_number("989-500-0006"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        state=CallAttemptState.ENDED_NORMALLY,
        ended_at=NOW - timedelta(hours=1),
    )
    db_session.add(attempt)
    db_session.commit()  # the call committed, but analysis registration never happened

    report = sweep(db_session, get_analysis_queue(), now=NOW)
    assert report.registered == 1 and report.published == 1
    assert _row(db_session, attempt).status == AnalysisStatus.PENDING
    assert sweep(db_session, get_analysis_queue(), now=NOW).registered == 0  # idempotent


def test_sweeper_lookback_is_bounded_it_is_not_a_historical_backfill(db_session, redis_client):
    campaign = Campaign(name="cp14b old")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="989-500-0007",
        normalized_phone_number=normalize_phone_number("989-500-0007"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    db_session.add(
        CallAttempt(
            contact_id=contact.id,
            attempt_number=1,
            state=CallAttemptState.ENDED_NORMALLY,
            ended_at=NOW - timedelta(days=60),
        )
    )
    db_session.commit()
    assert sweep(db_session, get_analysis_queue(), now=NOW).registered == 0


def test_sweeper_batches_are_bounded(db_session, redis_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "analysis_sweeper_batch_size", 2)
    attempts = [_admitted(db_session, phone=f"989-500-01{i:02d}")[2] for i in range(5)]
    redis_client.flushdb()
    later = _after_window()
    queue = get_analysis_queue()
    assert sweep(db_session, queue, now=later).published == 2
    assert sweep(db_session, queue, now=later).published == 2
    assert sweep(db_session, queue, now=later).published == 1  # drained over three sweeps
    assert len(attempts) == 5 and _jobs(redis_client) == 5


def test_redis_outage_marks_nothing_as_published_and_work_stays_discoverable(
    db_session, redis_client
):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0008")
    later = _after_window()

    class DownQueue:
        def enqueue(self, job):
            raise ConnectionError("redis down")

    report = sweep(db_session, DownQueue(), now=later)  # must not raise
    assert report.publish_error is True and report.published == 0
    assert _row(db_session, attempt).last_enqueued_at != later  # nothing falsely marked queued
    assert sweep(db_session, get_analysis_queue(), now=later).published == 1  # next sweep heals


def test_sweeper_finalizes_attempts_exhausted_rows_with_an_expired_lease(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0009")
    analysis_id = _row(db_session, attempt).id
    repo = CallAnalysisRepository(db_session)
    t = NOW
    for _ in range(3):
        repo.claim_for_processing(analysis_id, now=t, lease_seconds=120, max_attempts=3)
        t += timedelta(seconds=121)
    db_session.commit()
    report = sweep(db_session, get_analysis_queue(), now=t)
    assert report.finalized == 1
    assert _row(db_session, attempt).status == AnalysisStatus.FAILED


def test_sweeper_reports_pending_depth_and_oldest_age(db_session, redis_client):
    _admitted(db_session, phone="989-500-0010")
    report = sweep(db_session, get_analysis_queue(), now=NOW + timedelta(minutes=3))
    assert report.pending_count >= 1 and report.oldest_pending_age_seconds is not None


# ------------------------------------------------------------------ admission


def test_admission_registers_durably_but_publishes_only_after_the_commit(db_session, redis_client):
    campaign = Campaign(name="cp14b admission")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="989-500-0011",
        normalized_phone_number=normalize_phone_number("989-500-0011"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)
    assert _row(db_session, attempt).status == AnalysisStatus.PENDING  # durable row exists ...
    assert _jobs(redis_client) == 0  # ... but nothing is published before the commit
    db_session.commit()
    assert _jobs(redis_client) == 1  # published only once the state is committed


def test_redis_outage_during_admission_does_not_break_the_commit_and_sweeper_recovers(
    db_session, redis_client, monkeypatch
):
    def down():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.services.analysis.admission.get_analysis_queue", down)
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0012")  # must not raise
    row = _row(db_session, attempt)
    assert row.status == AnalysisStatus.PENDING  # durable despite the lost publish
    assert _jobs(redis_client) == 0

    monkeypatch.undo()
    later = _after_window()
    assert sweep(db_session, get_analysis_queue(), now=later).published == 1


def test_initial_delay_defers_the_first_claim_and_the_sweeper_publishes_when_due(
    db_session, redis_client, monkeypatch
):
    monkeypatch.setattr(get_settings(), "analysis_initial_delay_seconds", 60)
    _, _, attempt, _ = _admitted(db_session, phone="989-500-0013")
    row = _row(db_session, attempt)
    assert row.next_attempt_at is not None and row.next_attempt_at > datetime.now(UTC)
    redis_client.flushdb()
    queue = get_analysis_queue()
    assert sweep(db_session, queue, now=NOW).published == 0  # not due yet
    due = row.next_attempt_at + timedelta(seconds=1)
    assert sweep(db_session, queue, now=due).published == 1


# ------------------------------------------------------------------ budget


def _cfg(monkeypatch, *, provider="dograh_qa", cap=3.0, per=1.0):
    s = get_settings()
    monkeypatch.setattr(s, "analysis_llm_provider", provider)
    monkeypatch.setattr(s, "analysis_daily_estimated_spend_cap", cap)
    monkeypatch.setattr(s, "analysis_estimated_cost_per_analysis", per)


def _fresh_analysis(db):
    return _admitted(db, phone=f"989-600-{uuid.uuid4().int % 10000:04d}")[2]


def test_budget_boundary_allows_up_to_the_cap_and_defers_beyond(
    db_session, redis_client, monkeypatch
):
    _cfg(monkeypatch, cap=3.0, per=1.0)
    decisions = []
    for _ in range(4):
        attempt = _fresh_analysis(db_session)
        decisions.append(budget.reserve(db_session, _row(db_session, attempt), NOW))
    assert decisions == [BudgetDecision.RESERVED] * 3 + [BudgetDecision.CAP_REACHED]
    assert db_session.get(AnalysisBudgetDay, budget.budget_day_for(NOW)).reserved_cost == Decimal(3)


def test_reservation_is_idempotent_per_analysis(db_session, redis_client, monkeypatch):
    _cfg(monkeypatch, cap=5.0, per=2.0)
    row = _row(db_session, _fresh_analysis(db_session))
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.RESERVED
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.RESERVED  # a retry is free
    assert db_session.get(AnalysisBudgetDay, budget.budget_day_for(NOW)).reserved_cost == Decimal(2)


def test_estimate_larger_than_the_whole_cap_is_refused_even_on_the_first_insert(
    db_session, redis_client, monkeypatch
):
    _cfg(monkeypatch, cap=0.5, per=1.0)
    row = _row(db_session, _fresh_analysis(db_session))
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.CAP_REACHED
    assert db_session.get(AnalysisBudgetDay, budget.budget_day_for(NOW)) is None


def test_zero_cap_blocks_everything_and_unset_cap_fails_closed(
    db_session, redis_client, monkeypatch
):
    _cfg(monkeypatch, cap=0.0, per=1.0)
    row = _row(db_session, _fresh_analysis(db_session))
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.CAP_REACHED
    _cfg(monkeypatch, cap=None, per=None)
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.NOT_CONFIGURED


def test_mock_provider_is_not_gated_by_the_budget(db_session, redis_client, monkeypatch):
    _cfg(monkeypatch, provider="mock", cap=None, per=None)
    row = _row(db_session, _fresh_analysis(db_session))
    assert budget.reserve(db_session, row, NOW) == BudgetDecision.NOT_REQUIRED


def test_budget_day_rolls_over_in_the_configured_timezone(db_session, redis_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "budget_timezone", "Asia/Kolkata")
    just_before_ist_midnight = datetime(2026, 10, 10, 18, 29, tzinfo=UTC)  # 23:59 IST
    just_after = datetime(2026, 10, 10, 18, 31, tzinfo=UTC)  # 00:01 IST next day
    assert budget.budget_day_for(just_before_ist_midnight) != budget.budget_day_for(just_after)


def test_release_returns_capacity_and_never_goes_negative(db_session, redis_client, monkeypatch):
    _cfg(monkeypatch, cap=3.0, per=1.0)
    row = _row(db_session, _fresh_analysis(db_session))
    budget.reserve(db_session, row, NOW)
    budget.release(db_session, row.budget_day, row.reserved_cost)
    budget.release(db_session, row.budget_day, Decimal(5))  # over-release is clamped
    db_session.expire_all()
    assert db_session.get(AnalysisBudgetDay, row.budget_day).reserved_cost == Decimal(0)


def test_concurrent_workers_cannot_overspend_the_cap_through_a_race(redis_client, monkeypatch):
    """12 real threads, each on its own connection, race to reserve against a cap of 5.
    PostgreSQL's atomic conditional upsert must admit exactly 5 -- a local/Redis-only counter
    could not guarantee that."""
    from tests.conftest import TestSessionLocal

    _cfg(monkeypatch, cap=5.0, per=1.0)
    day = budget.budget_day_for(NOW)
    created: list[uuid.UUID] = []
    seed = TestSessionLocal()
    try:
        seed.execute(text("DELETE FROM analysis_budget_day WHERE day = :d"), {"d": day})
        campaign = Campaign(name="cp14b race")
        seed.add(campaign)
        seed.flush()
        for i in range(12):
            contact = Contact(
                campaign_id=campaign.id,
                phone_number=f"989-700-{i:04d}",
                normalized_phone_number=normalize_phone_number(f"989-700-{i:04d}"),
                status=ContactStatus.COMPLETED,
            )
            seed.add(contact)
            seed.flush()
            attempt = CallAttempt(
                contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
            )
            seed.add(attempt)
            seed.flush()
            analysis, _ = CallAnalysisRepository(seed).get_or_create_pending(
                call_attempt_id=attempt.id,
                contact_id=contact.id,
                campaign_id=campaign.id,
                conversation_session_id=None,
            )
            created.append(analysis.id)
        seed.commit()

        results: list[BudgetDecision] = []
        barrier = threading.Barrier(12)

        def reserve_one(analysis_id):
            session = TestSessionLocal()
            try:
                row = session.get(CallAnalysis, analysis_id)
                barrier.wait()
                decision = budget.reserve(session, row, NOW)
                session.commit()
                results.append(decision)
            finally:
                session.close()

        threads = [threading.Thread(target=reserve_one, args=(a,)) for a in created]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert results.count(BudgetDecision.RESERVED) == 5
        assert results.count(BudgetDecision.CAP_REACHED) == 7
        seed.expire_all()
        assert seed.get(AnalysisBudgetDay, day).reserved_cost == Decimal(5)  # never above the cap
    finally:
        seed.rollback()
        seed.execute(text("DELETE FROM analysis_budget_day WHERE day = :d"), {"d": day})
        seed.execute(text("DELETE FROM call_analysis WHERE id = ANY(:ids)"), {"ids": created})
        seed.execute(
            text(
                "DELETE FROM call_attempt WHERE contact_id IN "
                "(SELECT id FROM contact WHERE phone_number LIKE '989-700-%')"
            )
        )
        seed.execute(text("DELETE FROM contact WHERE phone_number LIKE '989-700-%'"))
        seed.execute(text("DELETE FROM campaign WHERE name = 'cp14b race'"))
        seed.commit()
        seed.close()


def test_budget_denial_keeps_the_row_pending_with_no_attempt_consumed(
    db_session, redis_client, monkeypatch
):
    _cfg(monkeypatch, cap=0.0, per=1.0)
    attempt = _fresh_analysis(db_session)
    analysis_id = _row(db_session, attempt).id
    claim = CallAnalysisRepository(db_session).claim_for_processing(
        analysis_id, now=NOW, lease_seconds=120, max_attempts=3
    )
    assert claim.analysis is None and claim.deferred_reason == "budget_cap_reached"
    row = _row(db_session, attempt)
    assert row.status == AnalysisStatus.PENDING and row.attempt_count == 0
    assert row.next_attempt_at == NOW + timedelta(
        seconds=get_settings().analysis_budget_recheck_seconds
    )  # low-frequency recheck, not a hot loop


def test_two_threads_claiming_one_row_yield_exactly_one_owner(redis_client):
    from tests.conftest import TestSessionLocal

    seed = TestSessionLocal()
    campaign = Campaign(name="cp14b claim race")
    seed.add(campaign)
    seed.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="989-800-0001",
        normalized_phone_number=normalize_phone_number("989-800-0001"),
        status=ContactStatus.COMPLETED,
    )
    seed.add(contact)
    seed.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    seed.add(attempt)
    seed.flush()
    analysis, _ = CallAnalysisRepository(seed).get_or_create_pending(
        call_attempt_id=attempt.id,
        contact_id=contact.id,
        campaign_id=campaign.id,
        conversation_session_id=None,
    )
    seed.commit()
    analysis_id = analysis.id
    winners: list[bool] = []
    barrier = threading.Barrier(2)

    def claim():
        session = TestSessionLocal()
        try:
            barrier.wait()
            result = CallAnalysisRepository(session).claim_for_processing(
                analysis_id, now=NOW, lease_seconds=120, max_attempts=3
            )
            session.commit()
            winners.append(result.analysis is not None)
        finally:
            session.close()

    try:
        threads = [threading.Thread(target=claim) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(winners) == [False, True]  # one owner, one loser -- never two
    finally:
        seed.rollback()
        seed.execute(text("DELETE FROM call_analysis WHERE id = :i"), {"i": analysis_id})
        seed.execute(text("DELETE FROM call_attempt WHERE id = :i"), {"i": attempt.id})
        seed.execute(text("DELETE FROM contact WHERE id = :i"), {"i": contact.id})
        seed.execute(text("DELETE FROM campaign WHERE id = :i"), {"i": campaign.id})
        seed.commit()
        seed.close()
