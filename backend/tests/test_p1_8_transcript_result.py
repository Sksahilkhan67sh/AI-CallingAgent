"""P1-8 -- a transcript that could not be fetched is NOT an empty transcript.

`fetch_transcript_lines` returned [] for a network error and for a genuinely empty transcript
alike, so a transient failure became "empty_conversation" and the analysis was SKIPPED for good.
Now the fetch returns OK(lines) / EMPTY / UNAVAILABLE(reason); the session remembers which, and
the analysis retries an UNAVAILABLE transcript (re-fetching it) and, if it never arrives, FAILS
with `transcript_unavailable` -- it never records "no transcript" as a final fact.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import AnalysisStatus
from app.services.analysis.admission import job_for
from app.services.analysis.factory import get_analysis_queue
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM
from app.services.analysis.worker import (
    AnalysisJobOutcome,
    process_claimed_job,
    process_one_analysis_job,
)
from app.services.telephony import transcript_fetch as tf
from tests.test_call_analysis_worker import _admitted

URL = "https://dograh.example.com/t.json?sig=abc"
LINES = [
    {"role": "assistant", "content": "Hello, this is a call about your enquiry."},
    {"role": "user", "content": "Yes, I'm interested, tell me more."},
]
_TARGET = tf._Target("https", "dograh.example.com", 443, "93.184.216.34", "/t.json")


# -- fetch classification ---------------------------------------------------------------------


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(tf, "resolve_target", lambda url: _TARGET)


def _body(monkeypatch, body: bytes):
    monkeypatch.setattr(tf, "_fetch_body", lambda target: body)


def test_a_list_of_lines_is_ok(routed, monkeypatch):
    _body(monkeypatch, json.dumps(LINES).encode())
    result = tf.fetch_transcript(URL)
    assert result.kind == tf.TranscriptKind.OK and result.lines == LINES and result.reason is None


@pytest.mark.parametrize("body", [b"[]", b'["not", "a", "dict"]'])
def test_a_valid_but_empty_transcript_is_empty_not_unavailable(routed, monkeypatch, body):
    _body(monkeypatch, body)
    result = tf.fetch_transcript(URL)
    assert result.kind == tf.TranscriptKind.EMPTY and result.lines == [] and result.reason is None


def _raise(exc):
    def run(target):
        raise exc

    return run


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (_raise(httpx.ConnectError("down")), "network_error"),
        (_raise(httpx.ReadTimeout("slow")), "network_error"),
        (_raise(OSError("reset")), "network_error"),
        (_raise(tf._Rejected("http_status")), "http_status"),
        (_raise(tf._Rejected("content_type")), "content_type"),
        (_raise(tf._Rejected("too_large")), "too_large"),
        (_raise(tf._Rejected("deadline")), "deadline"),
        (lambda target: b"this is not json", "invalid_json"),
        (lambda target: b'{"a": 1}', "unexpected_shape"),
    ],
)
def test_every_failure_is_unavailable_with_a_reason_never_empty(
    routed, monkeypatch, failure, reason
):
    monkeypatch.setattr(tf, "_fetch_body", failure)
    result = tf.fetch_transcript(URL)
    assert result.kind == tf.TranscriptKind.UNAVAILABLE
    assert result.reason == reason and result.lines == []
    assert URL not in repr(result)  # the URL (a signed link) is never part of a result


def test_a_blocked_url_is_unavailable_url_blocked(monkeypatch):
    monkeypatch.setattr(tf, "resolve_target", lambda url: None)
    result = tf.fetch_transcript("http://169.254.169.254/latest")
    assert (result.kind, result.reason) == (tf.TranscriptKind.UNAVAILABLE, "url_blocked")


def test_only_transient_reasons_are_retryable():
    assert tf.is_retryable("network_error") and tf.is_retryable("http_status")
    assert tf.is_retryable("deadline")
    for permanent in (
        "url_blocked",
        "content_type",
        "too_large",
        "invalid_json",
        "unexpected_shape",
    ):
        assert not tf.is_retryable(permanent)


def test_the_list_returning_wrapper_still_works_for_existing_callers(routed, monkeypatch):
    _body(monkeypatch, json.dumps(LINES).encode())
    assert tf.fetch_transcript_lines(URL) == LINES
    monkeypatch.setattr(tf, "_fetch_body", _raise(httpx.ConnectError("down")))
    assert tf.fetch_transcript_lines(URL) == []


# -- through the analysis pipeline --------------------------------------------------------------


def _analysis(db, attempt):
    return db.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()


def _store(db, session, result, url=URL):
    from app.services.telephony.transcript_store import apply_transcript_result

    apply_transcript_result(db, session, result, url)
    db.commit()


def _unavailable(reason):
    return tf.TranscriptResult(tf.TranscriptKind.UNAVAILABLE, [], reason)


def _refetch_returns(monkeypatch, result):
    from app.services.telephony import transcript_store

    calls = []

    def fetch(url):
        calls.append(url)
        return result

    monkeypatch.setattr(transcript_store, "fetch_transcript", fetch)
    return calls


def test_ok_transcript_is_stored_and_analysed(db_session, redis_client):
    _, _, attempt, session = _admitted(db_session, phone="989-800-0001", contact_text=None)
    _store(db_session, session, tf.TranscriptResult(tf.TranscriptKind.OK, LINES, None))
    llm = FakeAnalysisLLM()

    outcome = process_one_analysis_job(db_session, get_analysis_queue(), llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.COMPLETED and len(llm.calls) == 1
    db_session.refresh(session)
    assert (session.transcript_status, session.transcript_url) == ("ok", None)
    contents = [
        m.content
        for m in db_session.execute(
            select(ConversationMessage).where(ConversationMessage.session_id == session.id)
        ).scalars()
    ]
    assert len(contents) == 2


def test_a_genuinely_empty_transcript_is_still_skipped_as_empty(db_session, redis_client):
    _, _, attempt, session = _admitted(db_session, phone="989-800-0002", contact_text=None)
    _store(db_session, session, tf.TranscriptResult(tf.TranscriptKind.EMPTY, [], None))
    llm = FakeAnalysisLLM()

    outcome = process_one_analysis_job(db_session, get_analysis_queue(), llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.SKIPPED and not llm.calls
    analysis = _analysis(db_session, attempt)
    assert (analysis.status, analysis.error_code) == (AnalysisStatus.SKIPPED, "empty_conversation")
    db_session.refresh(session)
    assert (session.transcript_status, session.transcript_url) == ("empty", None)


def test_an_unavailable_transcript_is_deferred_then_recovers_when_it_arrives(
    db_session, redis_client, monkeypatch
):
    _, _, attempt, session = _admitted(db_session, phone="989-800-0003", contact_text=None)
    _store(db_session, session, _unavailable("network_error"))
    db_session.refresh(session)
    assert (session.transcript_status, session.transcript_unavailable_reason) == (
        "unavailable",
        "network_error",
    )
    assert session.transcript_url == URL, "kept (only while unavailable) so the retry can re-fetch"
    queue, llm = get_analysis_queue(), FakeAnalysisLLM()
    calls = _refetch_returns(monkeypatch, _unavailable("network_error"))

    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_RETRIABLE, "deferred, not skipped as empty"
    analysis = _analysis(db_session, attempt)
    assert analysis.status == AnalysisStatus.RETRY_WAIT
    assert analysis.error_code == "transcript_unavailable"
    assert calls == [URL] and not llm.calls

    # The transcript becomes available: the redelivered job re-fetches it and completes.
    _refetch_returns(monkeypatch, tf.TranscriptResult(tf.TranscriptKind.OK, LINES, None))
    queue.enqueue(job_for(analysis))
    read = queue.read_one("w2", block_ms=100)
    assert read is not None
    later = datetime.now(UTC) + timedelta(hours=1)
    outcome = process_claimed_job(db_session, queue, llm, *read, clock=lambda: later)
    db_session.commit()

    assert outcome == AnalysisJobOutcome.COMPLETED and len(llm.calls) == 1
    db_session.refresh(session)
    assert (session.transcript_status, session.transcript_url) == ("ok", None)
    assert db_session.query(ConversationMessage).filter_by(session_id=session.id).count() == 2


def test_a_transcript_that_never_arrives_fails_it_is_never_recorded_as_empty(
    db_session, redis_client, monkeypatch
):
    _, _, attempt, session = _admitted(db_session, phone="989-800-0004", contact_text=None)
    _store(db_session, session, _unavailable("http_status"))
    queue, llm = get_analysis_queue(), FakeAnalysisLLM()
    _refetch_returns(monkeypatch, _unavailable("http_status"))
    read = None
    outcome = process_one_analysis_job(db_session, queue, llm, consumer_name="w1")
    db_session.commit()
    analysis = _analysis(db_session, attempt)
    queue.enqueue(job_for(analysis))
    read = queue.read_one("w2", block_ms=100)
    assert read is not None
    message_id, job = read

    now = datetime.now(UTC)
    for _ in range(get_settings().analysis_max_attempts - 1):  # attempt 1 already ran above
        now += timedelta(hours=1)
        outcome = process_claimed_job(
            db_session, queue, llm, message_id, job, clock=lambda n=now: n
        )
        db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_TERMINAL
    db_session.refresh(analysis)
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.error_code == "transcript_unavailable"
    assert analysis.status != AnalysisStatus.SKIPPED and not llm.calls


def test_a_permanently_unavailable_transcript_fails_at_once_and_keeps_no_url(
    db_session, redis_client, monkeypatch
):
    _, _, attempt, session = _admitted(db_session, phone="989-800-0005", contact_text=None)
    _store(db_session, session, _unavailable("url_blocked"))
    db_session.refresh(session)
    assert session.transcript_status == "unavailable"
    assert session.transcript_url is None, "a URL that can never work is not retained"
    llm = FakeAnalysisLLM()
    calls = _refetch_returns(monkeypatch, _unavailable("url_blocked"))

    outcome = process_one_analysis_job(db_session, get_analysis_queue(), llm, consumer_name="w1")
    db_session.commit()

    assert outcome == AnalysisJobOutcome.FAILED_TERMINAL and not calls and not llm.calls
    analysis = _analysis(db_session, attempt)
    assert (analysis.status, analysis.error_code) == (
        AnalysisStatus.FAILED,
        "transcript_unavailable",
    )


# -- the webhook stores the right state ----------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "status", "reason", "keeps_url"),
    [
        (tf.TranscriptResult(tf.TranscriptKind.OK, LINES, None), "ok", None, False),
        (tf.TranscriptResult(tf.TranscriptKind.EMPTY, [], None), "empty", None, False),
        (_unavailable("network_error"), "unavailable", "network_error", True),
        (_unavailable("too_large"), "unavailable", "too_large", False),
    ],
    ids=["ok", "empty", "unavailable_transient", "unavailable_permanent"],
)
def test_the_webhook_records_the_outcome_on_the_session(
    db_session, redis_client, monkeypatch, result, status, reason, keeps_url
):
    from app.services.telephony import dograh_webhook_service as svc

    _, _, attempt, session = _admitted(db_session, phone="989-800-0006", contact_text=None)
    monkeypatch.setattr(svc, "fetch_transcript", lambda url: result)

    svc._populate_transcript(db_session, session, URL)
    db_session.commit()

    db_session.refresh(session)
    assert session.transcript_status == status
    assert session.transcript_unavailable_reason == reason
    assert (session.transcript_url == URL) is keeps_url


def test_no_transcript_url_means_no_transcript_state(db_session, redis_client):
    from app.services.telephony import dograh_webhook_service as svc

    _, _, attempt, session = _admitted(db_session, phone="989-800-0007", contact_text=None)
    svc._populate_transcript(db_session, session, None)
    db_session.refresh(session)
    assert session.transcript_status is None


# -- the retained URL has a lifecycle -------------------------------------------------------------


def test_erasure_clears_a_retained_transcript_url(db_session, redis_client):
    from app.services.data_lifecycle.erasure import _scrub_content

    _, contact, attempt, session = _admitted(db_session, phone="989-800-0008", contact_text=None)
    _store(db_session, session, _unavailable("network_error"))
    db_session.refresh(session)
    assert session.transcript_url == URL

    _scrub_content(db_session, [contact.id])
    db_session.commit()

    db_session.refresh(session)
    assert session.transcript_url is None


def test_unused_ids_are_real_uuids():
    assert isinstance(uuid.uuid4(), uuid.UUID) and ConversationSession is not None


def test_transcript_retention_clears_an_old_retained_link_and_keeps_a_recent_one(db_session):
    from app.models.conversation import ConversationSession as Session_
    from tests.cp15_helpers import days_ago, make_attempt, make_campaign, make_contact
    from tests.test_cp15a_retention import _run

    campaign = make_campaign(db_session)
    old_attempt = make_attempt(db_session, make_contact(db_session, campaign))
    new_attempt = make_attempt(db_session, make_contact(db_session, campaign))
    old = Session_(
        call_attempt_id=old_attempt.id,
        started_at=days_ago(31),
        transcript_status="unavailable",
        transcript_unavailable_reason="network_error",
        transcript_url=URL,
    )
    recent = Session_(
        call_attempt_id=new_attempt.id,
        started_at=days_ago(10),
        transcript_status="unavailable",
        transcript_unavailable_reason="network_error",
        transcript_url=URL,
    )
    db_session.add_all([old, recent])
    db_session.flush()

    result = _run(db_session)

    assert result.counts["transcript_links"] == 1
    db_session.refresh(old)
    db_session.refresh(recent)
    assert old.transcript_url is None and old.transcript_status == "unavailable"
    assert recent.transcript_url == URL
