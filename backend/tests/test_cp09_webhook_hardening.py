"""CP09 -- webhook auth, validation, idempotency, replay, state safety."""

import uuid

import pytest

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.processed_event import ProcessedEvent
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.services.phone import normalize_phone_number
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony import safe_fetch


@pytest.fixture(autouse=True)
def _clean_redis(redis_client):
    yield


URL = "/api/v1/webhooks/dograh/call-completed"


def _auth():
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


def _attempt(db, *, state=CallAttemptState.INITIATED, run_id="77", phone="555-980-0001"):
    campaign = Campaign(name="cp09 wh", status=CampaignStatus.ACTIVE)
    db.add(campaign)
    db.flush()
    db.add(RetryPolicy(campaign_id=campaign.id))
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DIALING,
        attempt_count=1,
    )
    db.add(contact)
    db.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider="dograh",
        provider_call_id=run_id,
        state=state,
    )
    db.add(attempt)
    db.flush()
    db.commit()  # rollback paths in the route must not discard test setup
    return contact, attempt


def _post(client, attempt, **fields):
    body = {"call_attempt_id": str(attempt.id), "call_status": "user_hangup", **fields}
    return client.post(URL, json=body, headers=_auth())


# ---- authentication -------------------------------------------------------


def test_unauthenticated_malformed_body_gets_401_not_422(client):
    r = client.post(URL, json={"nonsense": True})
    assert r.status_code == 401  # auth runs before payload validation


def test_non_ascii_credential_is_rejected_cleanly(client, db_session):
    _, a = _attempt(db_session)
    r = client.post(
        URL,
        json={"call_attempt_id": str(a.id)},
        headers={"X-API-Key": "pässword-ünicode".encode()},
    )
    assert r.status_code == 401


def test_unset_secret_fails_closed(client, db_session, monkeypatch):
    monkeypatch.setenv("DOGRAH_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    try:
        _, a = _attempt(db_session)
        r = client.post(URL, json={"call_attempt_id": str(a.id)}, headers={"X-API-Key": ""})
        assert r.status_code == 401
        r = client.post(
            URL, json={"call_attempt_id": str(a.id)}, headers={"Authorization": "Bearer "}
        )
        assert r.status_code == 401
    finally:
        get_settings.cache_clear()


# ---- payload validation ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"duration_seconds": -5},
        {"duration_seconds": "abc"},
        {"call_status": "x" * 5000},
        {"transcript_url": "file:///etc/passwd"},
        {"recording_url": "javascript:alert(1)"},
        {"workflow_run_id": "y" * 500},
    ],
)
def test_malformed_payloads_are_rejected_without_touching_state(client, db_session, bad):
    _, a = _attempt(db_session)
    r = _post(client, a, **bad)
    assert r.status_code == 422
    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED


def test_dograh_none_placeholders_are_treated_as_unknown(client, db_session):
    _, a = _attempt(db_session)
    r = _post(client, a, duration_seconds="None", transcript_url="", call_time="null")
    assert r.status_code == 200


# ---- idempotency / replay -------------------------------------------------


def test_duplicate_webhook_is_processed_exactly_once(client, db_session):
    contact, a = _attempt(db_session)
    first = _post(client, a, duration_seconds=60)
    second = _post(client, a, duration_seconds=60)
    third = _post(client, a, duration_seconds=60)

    assert first.json()["outcome"] == "ended_normally"
    assert second.json()["outcome"] == "already_processed"
    assert third.json()["outcome"] == "already_processed"
    transitions = (
        db_session.query(CallEvent).filter(CallEvent.event_type == "CALL_STATE_TRANSITION").count()
    )
    assert transitions == 2  # INITIATED->CONNECTED->ENDED_NORMALLY, once
    assert db_session.query(CallAnalysis).filter_by(call_attempt_id=a.id).count() == 1
    assert db_session.query(ProcessedEvent).filter_by(event_id=f"dograh:{a.id}").count() == 1


def test_processed_event_marker_alone_blocks_a_replay(client, db_session):
    """Even if state did not look terminal, the unique marker stops reprocessing."""
    _, a = _attempt(db_session)
    db_session.add(ProcessedEvent(event_id=f"dograh:{a.id}", event_type="dograh.call_completed"))
    db_session.flush()
    r = _post(client, a, duration_seconds=60)
    assert r.json()["outcome"] == "already_processed"
    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED


def test_replayed_webhook_with_wrong_run_id_is_rejected(client, db_session):
    _, a = _attempt(db_session, run_id="77")
    r = _post(client, a, workflow_run_id=999)
    assert r.status_code == 409
    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED
    assert db_session.query(ProcessedEvent).count() == 0


def test_run_id_is_adopted_when_the_trigger_response_was_lost(client, db_session):
    _, a = _attempt(db_session, run_id=None)
    r = _post(client, a, workflow_run_id=555, duration_seconds=30)
    assert r.status_code == 200
    db_session.refresh(a)
    assert a.provider_call_id == "555"


@pytest.mark.parametrize(
    "terminal",
    [
        CallAttemptState.ENDED_NORMALLY,
        CallAttemptState.FAILED_TO_CONNECT,
        CallAttemptState.DROPPED_MID_CALL,
    ],
)
def test_terminal_attempts_never_move_again(client, db_session, terminal):
    _, a = _attempt(db_session, state=terminal)
    r = _post(client, a, call_status="technical_error", duration_seconds=10)
    assert r.json()["outcome"] == "already_processed"
    db_session.refresh(a)
    assert a.state == terminal


def test_non_dograh_attempt_cannot_be_mutated_by_this_webhook(client, db_session):
    _, a = _attempt(db_session)
    a.provider = "mock"
    db_session.flush()
    assert _post(client, a).status_code == 409


def test_internal_failure_persists_nothing_and_is_not_acked(client, db_session, monkeypatch):
    _, a = _attempt(db_session)

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "app.services.telephony.dograh_webhook_service.apply_completion", boom
    )
    r = _post(client, a, duration_seconds=60)
    assert r.status_code == 500
    db_session.refresh(a)
    assert a.state == CallAttemptState.INITIATED
    assert db_session.query(ProcessedEvent).count() == 0  # retry/reconcile can still process it


# ---- classification: never-connected vs connected -------------------------


def test_provider_acceptance_then_no_answer_is_failed_to_connect_and_retries(client, db_session):
    _, a = _attempt(db_session)
    r = _post(client, a, call_status="no-answer", duration_seconds=0)
    assert r.json()["outcome"] == "failed_to_connect"
    db_session.refresh(a)
    assert a.state == CallAttemptState.FAILED_TO_CONNECT
    assert db_session.query(CallAnalysis).count() == 0  # no conversation, no analysis
    assert get_recovery_scheduler().pending_count() == 1  # retry #1 scheduled by RecoveryManager


def test_explicit_rejection_is_never_retried(client, db_session):
    _, a = _attempt(db_session)
    r = _post(client, a, call_status="call rejected by callee", duration_seconds=0)
    assert r.json()["outcome"] == "failed_to_connect"
    assert get_recovery_scheduler().pending_count() == 0


def test_connected_call_passes_through_connected_with_audit(client, db_session):
    _, a = _attempt(db_session)
    _post(client, a, duration_seconds=95)
    steps = [
        (e.payload["from"], e.payload["to"])
        for e in db_session.query(CallEvent).filter_by(event_type="CALL_STATE_TRANSITION")
    ]
    assert steps == [("Initiated", "Connected"), ("Connected", "EndedNormally")]


def test_mid_call_drop_retries_via_recovery_manager(client, db_session):
    _, a = _attempt(db_session)
    r = _post(client, a, call_status="technical_error_timeout", duration_seconds=20)
    assert r.json()["outcome"] == "dropped_mid_call"
    assert get_recovery_scheduler().pending_count() == 1


def test_opt_out_during_call_suppresses_and_never_retries(client, db_session):
    contact, a = _attempt(db_session)
    r = _post(client, a, duration_seconds=40, mapped_call_disposition="DNC")
    assert r.json()["outcome"] == "opted_out"
    db_session.refresh(contact)
    assert contact.status == ContactStatus.CLOSED
    assert db_session.query(Suppression).filter_by(contact_id=contact.id).count() == 1
    assert get_recovery_scheduler().pending_count() == 0
    assert db_session.query(CallAnalysis).count() == 0


# ---- transcript SSRF guard ------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://localhost/x",
        "ftp://example.com/t.json",
    ],
)
def test_transcript_fetch_refuses_internal_and_non_http_targets(url):
    with pytest.raises(safe_fetch.UnsafeUrl):
        safe_fetch.check_url(url, [])


def test_explicitly_allowed_private_host_is_permitted():
    assert safe_fetch.check_url("http://minio:9000/t.json", ["minio"]) == "minio"


def test_unsafe_transcript_url_does_not_fail_or_get_fetched(client, db_session, monkeypatch):
    called = []
    monkeypatch.setattr(
        "app.services.telephony.safe_fetch.httpx.get", lambda *a, **k: called.append(1)
    )
    _, a = _attempt(db_session)
    r = _post(client, a, duration_seconds=30, transcript_url="http://127.0.0.1/secret")
    assert r.status_code == 200
    assert called == []


def test_unknown_attempt_is_404(client):
    r = client.post(URL, json={"call_attempt_id": str(uuid.uuid4())}, headers=_auth())
    assert r.status_code == 404
