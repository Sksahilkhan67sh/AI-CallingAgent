"""CP14B -- worker ordering, leases, fencing, retry classification and the Dograh QA path.

Real PostgreSQL + Redis. Every provider interaction is a scripted in-process fake: no network,
no cost. Time is injected (`clock=`), never slept.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.models.call_analysis import AnalysisBudgetDay, CallAnalysis
from app.models.campaign import Campaign
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.services.analysis.admission import job_for
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.llm.base import (
    AnalysisLLM,
    AnalysisLLMPermanentError,
    AnalysisLLMProviderError,
    AnalysisLLMRateLimitError,
    AnalysisResultNotReady,
    AnalysisResultUnavailable,
)
from app.services.analysis.llm.dograh_qa import DograhQAAnalysisLLM
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM
from app.services.analysis.repository import CallAnalysisRepository
from app.services.analysis.worker import AnalysisJobOutcome as Outcome
from app.services.analysis.worker import process_claimed_job
from app.services.telephony.dograh_client import DograhClient
from tests.test_call_analysis_worker import _admitted
from tests.test_cp14b_qa_contract_and_adapter import GOOD

GOOD_RESULT = FakeAnalysisLLM().analyze(["contact: I'm interested"], brand_name="")
NOW = datetime.now(UTC)


class ScriptedLLM(AnalysisLLM):
    provider_name = "mock"
    model_name = "scripted"

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors) or [GOOD_RESULT]
        self.calls = 0
        self.on_call = None

    def analyze(self, transcript_lines, *, brand_name, context=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        behavior = self.behaviors.pop(0) if len(self.behaviors) > 1 else self.behaviors[0]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def _row(db, attempt):
    db.expire_all()
    return db.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()


def _run(db, llm, *, clock_at=None, rng=lambda: 1.0):
    queue = get_analysis_queue()
    read = queue.read_one("w1", block_ms=100)
    assert read is not None, "no job was published"
    message_id, job = read
    clock = (lambda: clock_at) if clock_at else (lambda: datetime.now(UTC))
    outcome = process_claimed_job(db, queue, llm, message_id, job, clock=clock, rng=rng)
    db.commit()
    return outcome, message_id, job


def _pending(redis_client):
    queue = get_analysis_queue()
    return redis_client.xpending(queue.stream_key, queue.group)["pending"]


# ------------------------------------------------------------------ ordering


def test_job_is_acked_only_after_the_result_is_committed(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0001")
    queue = get_analysis_queue()
    events: list[str] = []
    real_commit, real_ack = db_session.commit, queue.ack

    def commit():
        events.append("commit")
        return real_commit()

    def ack(message_id):
        # At ack time the COMPLETED state must already be flushed + committed.
        db_session.expire_all()
        row = db_session.query(CallAnalysis).filter_by(call_attempt_id=attempt.id).one()
        events.append(f"ack(status={row.status.value})")
        return real_ack(message_id)

    db_session.commit = commit
    queue.ack = ack
    read = queue.read_one("w1", block_ms=100)
    message_id, job = read
    outcome = process_claimed_job(db_session, queue, ScriptedLLM(), message_id, job)

    assert outcome == Outcome.COMPLETED
    assert events[-2:] == ["commit", "ack(status=completed)"]  # commit strictly precedes ack
    assert events.count("commit") >= 3  # claim, input-load/lease, result -- never one big txn


def test_no_database_transaction_is_open_during_the_provider_call(db_session, redis_client):
    _admitted(db_session, phone="989-400-0002")
    llm = ScriptedLLM()
    seen = {}
    llm.on_call = lambda: seen.setdefault("in_txn", db_session.in_transaction())
    outcome, *_ = _run(db_session, llm)
    assert outcome == Outcome.COMPLETED
    assert seen["in_txn"] is False  # no txn, hence no row lock, while "waiting on Dograh"


def test_failed_result_persistence_does_not_ack_and_is_recovered_after_lease_expiry(
    db_session, redis_client, monkeypatch
):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0003")
    queue = get_analysis_queue()
    message_id, job = queue.read_one("w1", block_ms=100)

    def explode(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(CallAnalysisRepository, "complete", explode)
    with pytest.raises(RuntimeError):
        process_claimed_job(db_session, queue, ScriptedLLM(), message_id, job)

    assert _pending(redis_client) == 1  # NOT acked -- still owed to a consumer
    row = _row(db_session, attempt)
    assert row.status == AnalysisStatus.PROCESSING and row.claim_token is not None

    # Recovery: once the lease has expired, another worker re-claims and completes it.
    monkeypatch.undo()
    later = datetime.now(UTC) + timedelta(seconds=get_settings().analysis_lease_seconds + 5)
    outcome = process_claimed_job(
        db_session, queue, ScriptedLLM(), message_id, job, clock=lambda: later
    )
    db_session.commit()
    assert outcome == Outcome.COMPLETED
    assert _row(db_session, attempt).attempt_count == 2


def test_failed_retry_state_persistence_does_not_ack(db_session, redis_client, monkeypatch):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0004")
    queue = get_analysis_queue()
    message_id, job = queue.read_one("w1", block_ms=100)

    def explode(*a, **k):
        raise RuntimeError("cannot write retry state")

    monkeypatch.setattr(CallAnalysisRepository, "schedule_retry", explode)
    with pytest.raises(RuntimeError):
        process_claimed_job(
            db_session, queue, ScriptedLLM(AnalysisLLMProviderError("x")), message_id, job
        )
    assert _pending(redis_client) == 1  # the original job stays recoverable


# ------------------------------------------------------------------ lease + fencing


def test_stale_worker_cannot_overwrite_a_newer_workers_result(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0005")
    repo = CallAnalysisRepository(db_session)
    analysis_id = _row(db_session, attempt).id
    lease = get_settings().analysis_lease_seconds

    first = repo.claim_for_processing(analysis_id, now=NOW, lease_seconds=lease, max_attempts=3)
    # A's lease expires; B takes over with a NEW fencing token and finishes.
    later = NOW + timedelta(seconds=lease + 1)
    second = repo.claim_for_processing(analysis_id, now=later, lease_seconds=lease, max_attempts=3)
    assert second.analysis is not None and second.claim_token != first.claim_token
    assert repo.complete(analysis_id, second.claim_token, now=later, summary="B wins") is True

    # A finally returns: every fenced write is refused.
    assert repo.complete(analysis_id, first.claim_token, now=later, summary="A stale") is False
    assert (
        repo.schedule_retry(
            analysis_id, first.claim_token, now=later, error_code="x", next_attempt_at=later
        )
        is False
    )
    assert repo.fail(analysis_id, first.claim_token, now=later, error_code="x") is False
    row = _row(db_session, attempt)
    assert row.summary == "B wins" and row.status == AnalysisStatus.COMPLETED


def test_fencing_token_alone_blocks_a_stale_worker_while_the_new_owner_is_still_running(
    db_session, redis_client
):
    """The newer worker has NOT finished (row is PROCESSING under token B). The old worker's
    writes must still be refused -- by the fencing token, not merely by a terminal status."""
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0030")
    repo = CallAnalysisRepository(db_session)
    analysis_id = _row(db_session, attempt).id
    lease = get_settings().analysis_lease_seconds
    a = repo.claim_for_processing(analysis_id, now=NOW, lease_seconds=lease, max_attempts=3)
    later = NOW + timedelta(seconds=lease + 1)
    b = repo.claim_for_processing(analysis_id, now=later, lease_seconds=lease, max_attempts=3)

    assert repo.complete(analysis_id, a.claim_token, now=later, summary="A stale") is False
    assert repo.fail(analysis_id, a.claim_token, now=later, error_code="x") is False
    assert repo.skip(analysis_id, a.claim_token, now=later, error_code="x") is False
    assert repo.renew_lease(analysis_id, a.claim_token, now=later, lease_seconds=lease) is False
    row = _row(db_session, attempt)
    assert row.status == AnalysisStatus.PROCESSING and row.claim_token == b.claim_token
    assert row.summary is None  # B is still the sole owner
    assert repo.complete(analysis_id, b.claim_token, now=later, summary="B") is True


def test_worker_whose_claim_is_stolen_mid_request_writes_nothing(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0006")
    analysis_id = _row(db_session, attempt).id
    llm = ScriptedLLM()

    def steal():  # while A is "waiting on the provider", its lease lapses and B finishes
        repo = CallAnalysisRepository(db_session)
        later = datetime.now(UTC) + timedelta(hours=1)
        claim = repo.claim_for_processing(analysis_id, now=later, lease_seconds=120, max_attempts=3)
        assert repo.complete(analysis_id, claim.claim_token, now=later, summary="newer") is True
        db_session.commit()

    llm.on_call = steal
    outcome, *_ = _run(db_session, llm)
    assert outcome == Outcome.STALE
    assert _row(db_session, attempt).summary == "newer"  # A's result discarded
    assert _pending(redis_client) == 0  # PostgreSQL holds the truth, so acking is safe


def test_live_lease_blocks_a_duplicate_worker_and_is_not_processed_twice(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0007")
    repo = CallAnalysisRepository(db_session)
    analysis_id = _row(db_session, attempt).id
    repo.claim_for_processing(analysis_id, now=NOW, lease_seconds=120, max_attempts=3)
    db_session.commit()

    llm = ScriptedLLM()
    outcome, *_ = _run(db_session, llm, clock_at=NOW + timedelta(seconds=10))
    assert outcome == Outcome.NOT_CLAIMABLE
    assert llm.calls == 0  # no duplicate provider request while the owner's lease is live
    assert _row(db_session, attempt).status == AnalysisStatus.PROCESSING


def test_crashed_workers_row_is_recovered_without_a_duplicate_final_result(
    db_session, redis_client
):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0008")
    repo = CallAnalysisRepository(db_session)
    analysis_id = _row(db_session, attempt).id
    repo.claim_for_processing(analysis_id, now=NOW, lease_seconds=120, max_attempts=3)
    db_session.commit()  # ... and the worker "dies" here

    llm = ScriptedLLM()
    outcome, *_ = _run(db_session, llm, clock_at=NOW + timedelta(seconds=121))
    assert outcome == Outcome.COMPLETED
    assert db_session.query(CallAnalysis).filter_by(call_attempt_id=attempt.id).count() == 1
    assert _row(db_session, attempt).attempt_count == 2


def test_crashed_on_its_last_attempt_becomes_terminal_not_stuck(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0009")
    analysis_id = _row(db_session, attempt).id
    repo = CallAnalysisRepository(db_session)
    t = NOW
    for _ in range(get_settings().analysis_max_attempts):
        claim = repo.claim_for_processing(analysis_id, now=t, lease_seconds=120, max_attempts=3)
        assert claim.analysis is not None
        t += timedelta(seconds=121)  # each worker dies; the lease lapses
    db_session.commit()

    outcome, *_ = _run(db_session, ScriptedLLM(), clock_at=t)
    assert outcome == Outcome.FAILED_TERMINAL
    row = _row(db_session, attempt)
    assert row.status == AnalysisStatus.FAILED
    assert row.error_code == "lease_expired_attempts_exhausted"


# ------------------------------------------------------------------ retry classification


def test_transient_error_waits_in_retry_wait_with_jittered_backoff(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0010")
    outcome, *_ = _run(
        db_session, ScriptedLLM(AnalysisLLMProviderError("x")), clock_at=NOW, rng=lambda: 1.0
    )
    row = _row(db_session, attempt)
    assert outcome == Outcome.FAILED_RETRIABLE and row.status == AnalysisStatus.RETRY_WAIT
    assert row.next_attempt_at == NOW + timedelta(seconds=30)  # base * 2**0 * jitter(1.0)


def test_rate_limit_honours_retry_after(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0011")
    err = AnalysisLLMRateLimitError("slow down", retry_after_seconds=600)
    _run(db_session, ScriptedLLM(err), clock_at=NOW, rng=lambda: 0.0)
    row = _row(db_session, attempt)
    assert row.error_code == "provider_rate_limited"
    assert row.next_attempt_at == NOW + timedelta(seconds=600)


def test_permanent_error_fails_immediately_without_retry(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0012")
    llm = ScriptedLLM(AnalysisLLMPermanentError("bad key"))
    outcome, *_ = _run(db_session, llm)
    row = _row(db_session, attempt)
    assert outcome == Outcome.FAILED_TERMINAL and row.status == AnalysisStatus.FAILED
    assert row.error_code == "provider_permanent_error" and llm.calls == 1


def test_errors_and_logs_never_leak_exception_text(db_session, redis_client, caplog):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0013")
    secret = "key=sk-LIVE-123 phone=9891234567 transcript: I want it"
    with caplog.at_level("DEBUG"):
        _run(db_session, ScriptedLLM(RuntimeError(secret)))
    row = _row(db_session, attempt)
    assert row.error_code == "provider_unavailable" and row.error_message is None
    assert "sk-LIVE" not in caplog.text and "9891234567" not in caplog.text


def test_analysis_failure_never_touches_call_or_contact_state(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="989-400-0014")
    before = (attempt.state, attempt.ended_at, contact.status, contact.attempt_count)
    for _ in range(get_settings().analysis_max_attempts):
        _run(db_session, ScriptedLLM(AnalysisLLMProviderError("x")), clock_at=None)
        db_session.query(CallAnalysis).update({"next_attempt_at": NOW - timedelta(days=1)})
        queue = get_analysis_queue()
        queue.enqueue(job_for(_row(db_session, attempt)))
    db_session.expire_all()
    after = (attempt.state, attempt.ended_at, contact.status, contact.attempt_count)
    assert before == after  # no redial, no retry-count change, no status change


def test_provider_requests_per_analysis_are_bounded_by_the_attempt_cap(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0015")
    llm = ScriptedLLM(AnalysisLLMProviderError("x"))
    queue = get_analysis_queue()
    message_id, job = queue.read_one("w1", block_ms=100)
    t = NOW
    for _ in range(10):  # far more deliveries than attempts
        t += timedelta(hours=1)
        process_claimed_job(db_session, queue, llm, message_id, job, clock=lambda now=t: now)
        db_session.commit()
    assert llm.calls == get_settings().analysis_max_attempts  # no nested/unbounded retry


# ------------------------------------------------------------------ QA readiness


def test_qa_not_ready_polls_without_consuming_attempts_then_skips_at_the_deadline(
    db_session, redis_client
):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0016")
    created = _row(db_session, attempt).created_at
    outcome, *_ = _run(
        db_session,
        ScriptedLLM(AnalysisResultNotReady("x")),
        clock_at=created + timedelta(seconds=5),
    )
    row = _row(db_session, attempt)
    assert outcome == Outcome.FAILED_RETRIABLE and row.status == AnalysisStatus.RETRY_WAIT
    assert row.error_code == "qa_not_ready" and row.attempt_count == 0  # attempt refunded

    deadline = get_settings().analysis_qa_ready_deadline_seconds
    queue = get_analysis_queue()
    queue.enqueue(job_for(row))
    outcome, *_ = _run(
        db_session,
        ScriptedLLM(AnalysisResultNotReady("x")),
        clock_at=created + timedelta(seconds=deadline + 60),
    )
    row = _row(db_session, attempt)
    assert outcome == Outcome.SKIPPED and row.status == AnalysisStatus.SKIPPED
    assert row.error_code == "qa_result_not_produced"  # "never ran", distinct from "not yet"


def test_qa_known_unavailable_is_skipped_immediately(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0017")
    outcome, *_ = _run(db_session, ScriptedLLM(AnalysisResultUnavailable("short call")))
    row = _row(db_session, attempt)
    assert outcome == Outcome.SKIPPED and row.error_code == "qa_unavailable"
    assert row.summary is None and row.interest_status is None  # never a fabricated outcome


# ------------------------------------------------------------------ input validation


def test_tampered_relationship_is_refused_before_any_provider_call(db_session, redis_client):
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0018")
    other = Campaign(name="someone else's campaign")
    db_session.add(other)
    db_session.flush()
    db_session.query(CallAnalysis).update({"campaign_id": other.id})
    db_session.commit()
    llm = ScriptedLLM()
    outcome, *_ = _run(db_session, llm)
    assert outcome == Outcome.SKIPPED and llm.calls == 0
    assert _row(db_session, attempt).error_code == "invalid_call_relationship"


def test_call_that_is_no_longer_eligible_is_skipped(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="989-400-0019")
    contact.status = ContactStatus.CLOSED  # e.g. opted out after admission
    db_session.commit()
    llm = ScriptedLLM()
    outcome, *_ = _run(db_session, llm)
    assert outcome == Outcome.SKIPPED and llm.calls == 0
    assert _row(db_session, attempt).error_code == "not_eligible"


def test_attempt_state_is_untouched_by_a_skipped_analysis(db_session, redis_client):
    _, contact, attempt, _ = _admitted(db_session, phone="989-400-0020", contact_text=None)
    _run(db_session, ScriptedLLM())
    db_session.refresh(attempt)
    assert attempt.state == CallAttemptState.ENDED_NORMALLY


# ------------------------------------------------------------------ Dograh QA end to end


def _configure_dograh(monkeypatch, *, cap="10", per="1"):
    s = get_settings()
    monkeypatch.setattr(s, "analysis_llm_provider", "dograh_qa")
    monkeypatch.setattr(s, "analysis_daily_estimated_spend_cap", float(cap) if cap else None)
    monkeypatch.setattr(s, "analysis_estimated_cost_per_analysis", float(per) if per else None)


def _dograh_llm(monkeypatch, run):
    client = DograhClient(base_url="https://d.example.com", api_key="k", trigger_uuid="t")
    monkeypatch.setattr(client, "get_run", lambda **kw: run)
    return DograhQAAnalysisLLM(client)


def test_dograh_qa_end_to_end_with_deterministic_score_and_cost_recording(
    db_session, redis_client, monkeypatch
):
    _configure_dograh(monkeypatch)
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0021")
    attempt.provider, attempt.provider_call_id, attempt.dograh_workflow_id = "dograh", "9", 3
    attempt.duration_seconds = 90.0
    db_session.commit()

    run = {"is_completed": True, "annotations": {"qa_1": GOOD}, "cost_info": {"charge_usd": 0.31}}
    outcome, *_ = _run(db_session, _dograh_llm(monkeypatch, run))
    row = _row(db_session, attempt)

    assert outcome == Outcome.COMPLETED and row.status == AnalysisStatus.COMPLETED
    assert (row.model_provider, row.model_name) == ("dograh", "dograh-qa-node")
    assert row.lead_score is not None and row.lead_score > 0  # deterministic CP06 scorer
    assert row.observed_run_cost_usd == Decimal("0.310000")
    assert row.reserved_cost == Decimal("1.000000")  # estimate reserved, separate from observed
    assert db_session.get(AnalysisBudgetDay, row.budget_day).reserved_cost == Decimal("1")


def test_dograh_qa_without_run_identifiers_is_skipped_not_retried(
    db_session, redis_client, monkeypatch
):
    _configure_dograh(monkeypatch)
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0022")  # no provider_call_id
    outcome, *_ = _run(db_session, _dograh_llm(monkeypatch, {}))
    row = _row(db_session, attempt)
    assert outcome == Outcome.SKIPPED and row.error_code == "missing_provider_run_id"
    assert row.reserved_cost == 0  # the reservation was released: QA never ran for it


def test_workflow_id_falls_back_to_configuration(db_session, redis_client, monkeypatch):
    _configure_dograh(monkeypatch)
    monkeypatch.setattr(get_settings(), "dograh_workflow_id", 77)
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0023")
    attempt.provider_call_id = "5"
    db_session.commit()
    seen = {}
    client = DograhClient(base_url="https://d.example.com", api_key="k", trigger_uuid="t")
    monkeypatch.setattr(
        client, "get_run", lambda **kw: seen.update(kw) or {"annotations": {"q": GOOD}}
    )
    _run(db_session, DograhQAAnalysisLLM(client))
    assert seen == {"workflow_id": 77, "run_id": 5}


def test_budget_not_configured_fails_closed_and_keeps_the_work_pending(
    db_session, redis_client, monkeypatch
):
    _configure_dograh(monkeypatch, cap="", per="")
    _, _, attempt, _ = _admitted(db_session, phone="989-400-0024")
    attempt.provider_call_id, attempt.dograh_workflow_id = "9", 3
    db_session.commit()
    llm = _dograh_llm(monkeypatch, {"annotations": {"q": GOOD}})
    outcome, *_ = _run(db_session, llm)
    row = _row(db_session, attempt)
    assert outcome == Outcome.DEFERRED
    assert row.status == AnalysisStatus.PENDING and row.attempt_count == 0  # nothing consumed
    assert row.error_code == "budget_not_configured" and row.next_attempt_at > datetime.now(UTC)
    assert row.claim_token is None and row.lease_expires_at is None  # claim was undone
    assert _pending(redis_client) == 0
