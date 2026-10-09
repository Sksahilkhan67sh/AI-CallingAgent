"""Checkpoint 10 -- provider status normalization, correlation validation and
the webhook lifecycle. All payloads are hand-written test fixtures, NOT
captured from a real Dograh instance (LIVE DOGRAH E2E = UNVERIFIED)."""

import uuid

import pytest

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent, ConversationSession
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    NeverConnectedFailureReason,
)
from app.services.telephony.dograh_outcome import classify_call_outcome as _normalize
from app.services.telephony.dograh_reconciliation import AMBIGUOUS_TRIGGER_EVENT
from app.services.telephony.dograh_webhook_service import _is_safe_transcript_url
from tests.phone_helpers import normalize_phone_number

S = CallAttemptState
URL = "/api/v1/webhooks/dograh/call-completed"


# --------------------------------------------------------------------------
# Normalization (pure)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected",
    [
        ("user_hangup", S.ENDED_NORMALLY),
        ("USER_HANGUP", S.ENDED_NORMALLY),
        ("  Completed ", S.ENDED_NORMALLY),
        ("end_call", S.ENDED_NORMALLY),
        ("no_answer", S.FAILED_TO_CONNECT),
        ("No Answer", S.FAILED_TO_CONNECT),
        ("no-answer", S.FAILED_TO_CONNECT),  # Dograh's real hyphenated spelling
        ("busy", S.FAILED_TO_CONNECT),
        ("failed", S.FAILED_TO_CONNECT),
        ("pipeline_error", S.DROPPED_MID_CALL),
        ("unexpected_error", S.DROPPED_MID_CALL),
    ],
)
def test_known_status_variants(status, expected):
    c = _normalize(status, None)
    assert (c.state, c.recognized) == (expected, True)


@pytest.mark.parametrize(
    "status", [None, "", "   ", "mystery", "incomplete", "?", "rejected", "invalid_number"]
)
def test_unknown_or_empty_status_is_never_success_and_never_a_connected_call(status):
    c = _normalize(status, None)
    assert c.state == S.FAILED_TO_CONNECT
    assert c.never_connected_reason == NeverConnectedFailureReason.PROVIDER_ERROR
    assert c.recognized is False


@pytest.mark.parametrize("status", ["initiated", "ringing", "in-progress", "answered"])
def test_non_final_status_takes_no_decision(status):
    assert _normalize(status, None).non_final is True


def test_business_disposition_does_not_change_a_recognized_status():
    c = _normalize("user_hangup", "callback_requested")
    assert (c.state, c.basis) == (S.ENDED_NORMALLY, "status")


@pytest.mark.parametrize(
    "status,disposition",
    [
        ("user_hangup", "no_answer"),  # claims success vs. never connected
        ("completed", "busy"),
        ("no_answer", "completed"),  # claims never connected vs. success
        ("busy", "user_hangup"),
        ("user_hangup", "failed"),
    ],
)
def test_conflicting_status_and_disposition_is_not_trusted_as_success(status, disposition):
    c = _normalize(status, disposition)
    assert (c.state, c.recognized, c.basis) == (S.FAILED_TO_CONNECT, False, "conflict")
    assert c.never_connected_reason == NeverConnectedFailureReason.PROVIDER_ERROR


def test_failure_disposition_may_supply_the_outcome_when_status_is_unknown():
    c = _normalize("mystery", "busy")
    assert (c.state, c.never_connected_reason, c.basis) == (
        S.FAILED_TO_CONNECT,
        NeverConnectedFailureReason.BUSY,
        "disposition",
    )


def test_normal_completion_disposition_never_rescues_an_unknown_status():
    c = _normalize("mystery", "completed")
    assert (c.state, c.recognized) == (S.FAILED_TO_CONNECT, False)
    assert _normalize(None, "user_hangup").recognized is False


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _attempt(db, *, phone, run_id="99", provider="dograh", state=S.INITIATED, campaign=None):
    campaign = campaign or Campaign(name="CP10 webhook", status=CampaignStatus.ACTIVE)
    if campaign.id is None:
        db.add(campaign)
        db.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DIALING,
    )
    db.add(contact)
    db.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider=provider,
        provider_call_id=run_id,
        state=state,
    )
    db.add(attempt)
    db.flush()
    db.commit()
    return campaign, contact, attempt


def _post(client, attempt, **fields):
    body = {"call_attempt_id": str(attempt.id), "call_status": "user_hangup", **fields}
    return client.post(
        URL, json=body, headers={"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}
    )


def _audits(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).count()


# --------------------------------------------------------------------------
# Lifecycle through the endpoint
# --------------------------------------------------------------------------
def test_conflicting_webhook_creates_no_session_no_analysis_and_goes_to_recovery(
    client, db_session
):
    _, contact, attempt = _attempt(db_session, phone="989-980-0001")

    response = _post(client, attempt, workflow_run_id=99, call_disposition="no_answer")

    assert response.status_code == 200 and response.json()["outcome"] == "never_connected"
    db_session.refresh(attempt)
    assert attempt.state == S.FAILED_TO_CONNECT
    assert attempt.connection_failure_reason == NeverConnectedFailureReason.PROVIDER_ERROR
    assert db_session.query(ConversationSession).count() == 0
    assert db_session.query(CallAnalysis).count() == 0
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "call_attempt.never_connected_via_dograh_webhook")
        .one()
    )
    assert audit.event_metadata["basis"] == "conflict"


def test_unknown_status_webhook_is_never_a_successful_completion(client, db_session):
    _, contact, attempt = _attempt(db_session, phone="989-980-0002")

    response = _post(client, attempt, workflow_run_id=99, call_status="brand_new_value")
    assert response.status_code == 200

    db_session.refresh(attempt)
    db_session.refresh(contact)
    assert attempt.state != S.ENDED_NORMALLY and contact.status != ContactStatus.COMPLETED
    assert db_session.query(CallAnalysis).count() == 0


def test_matching_correlation_ids_are_accepted(client, db_session):
    campaign, contact, attempt = _attempt(db_session, phone="989-980-0003")
    response = _post(
        client,
        attempt,
        workflow_run_id=99,
        contact_id=str(contact.id),
        campaign_id=str(campaign.id),
    )
    assert response.status_code == 200 and response.json()["outcome"] == "ended_normally"


@pytest.mark.parametrize(
    "fields,problem",
    [
        ({"workflow_run_id": 12345}, "run_mismatch"),
        ({"workflow_run_id": 99, "contact_id": str(uuid.uuid4())}, "contact_mismatch"),
        ({"workflow_run_id": 99, "campaign_id": str(uuid.uuid4())}, "campaign_mismatch"),
    ],
)
def test_wrong_correlation_is_rejected_audited_and_mutates_nothing(
    client, db_session, fields, problem
):
    _, contact, attempt = _attempt(db_session, phone="989-980-0004")

    response = _post(client, attempt, **fields)

    assert response.status_code == 409
    db_session.refresh(attempt)
    db_session.refresh(contact)
    assert attempt.state == S.INITIATED and attempt.provider_call_id == "99"
    assert contact.status == ContactStatus.DIALING
    assert db_session.query(CallAnalysis).count() == 0
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "dograh.webhook_correlation_rejected")
        .one()
    )
    assert row.event_metadata["problem"] == problem


def test_wrong_run_cannot_touch_an_already_terminal_attempt_either(client, db_session):
    _, _, attempt = _attempt(db_session, phone="989-980-0005", state=S.ENDED_NORMALLY)
    assert _post(client, attempt, workflow_run_id=555).status_code == 409
    assert _audits(db_session, "dograh.webhook_correlation_rejected") == 1


def test_a_dograh_webhook_cannot_mutate_a_native_provider_attempt(client, db_session):
    _, _, attempt = _attempt(db_session, phone="989-980-0006", run_id="mock-1", provider="mock")
    assert _post(client, attempt).status_code == 409
    db_session.refresh(attempt)
    assert attempt.state == S.INITIATED


def test_a_run_already_owned_by_another_attempt_cannot_be_adopted(client, db_session):
    campaign, _, owner = _attempt(db_session, phone="989-980-0007", run_id="77")
    _, _, other = _attempt(db_session, phone="989-980-0008", run_id=None, campaign=campaign)

    assert _post(client, other, workflow_run_id=77).status_code == 409

    db_session.refresh(other)
    assert other.provider_call_id is None and other.state == S.INITIATED


def test_completion_webhook_arriving_before_trigger_response_is_handled(client, db_session):
    """Webhook race: the attempt was claimed (INITIATED, no run id yet) when the
    completion webhook lands. It must complete the attempt AND remember the run."""
    _, _, attempt = _attempt(db_session, phone="989-980-0009", run_id=None, provider=None)

    response = _post(client, attempt, workflow_run_id=4242)

    assert response.status_code == 200 and response.json()["outcome"] == "ended_normally"
    db_session.refresh(attempt)
    assert (attempt.provider, attempt.provider_call_id) == ("dograh", "4242")
    assert attempt.state == S.ENDED_NORMALLY


def test_duplicate_webhook_is_a_noop_and_creates_one_analysis(client, db_session):
    _, contact, attempt = _attempt(db_session, phone="989-980-0010")

    first = _post(client, attempt, workflow_run_id=99)
    second = _post(client, attempt, workflow_run_id=99)

    assert first.json()["outcome"] == "ended_normally"
    assert second.json()["outcome"] == "already_processed"
    assert db_session.query(ConversationSession).count() == 1


def test_webhook_after_ambiguous_trigger_adopts_the_run_without_a_second_call(client, db_session):
    """CP09 reconciliation protection still holds under the CP10 correlation checks."""
    _, contact, attempt = _attempt(
        db_session, phone="989-980-0011", run_id=None, state=S.FAILED_TO_CONNECT
    )
    attempt.provider = "dograh"
    db_session.add(CallEvent(call_attempt_id=attempt.id, event_type=AMBIGUOUS_TRIGGER_EVENT))
    db_session.commit()

    response = _post(client, attempt, workflow_run_id=31337)

    assert response.status_code == 200 and response.json()["outcome"] == "ended_normally"
    db_session.refresh(attempt)
    assert attempt.provider_call_id == "31337" and attempt.state == S.ENDED_NORMALLY


# --------------------------------------------------------------------------
# Authentication / validation ordering
# --------------------------------------------------------------------------
def test_unauthenticated_caller_gets_401_not_schema_feedback(client, db_session):
    assert client.post(URL, json={"nonsense": True}).status_code == 401


def test_non_ascii_credential_is_a_clean_401(client, db_session):
    _, _, attempt = _attempt(db_session, phone="989-980-0012")
    response = client.post(
        URL,
        json={"call_attempt_id": str(attempt.id), "call_status": "user_hangup"},
        headers={"X-API-Key": "secrët-ключ".encode()},
    )
    assert response.status_code == 401


def test_malformed_authenticated_payload_mutates_nothing(client, db_session):
    _, _, attempt = _attempt(db_session, phone="989-980-0013")
    response = client.post(
        URL,
        json={"call_attempt_id": str(attempt.id), "recording_url": "file:///etc/passwd"},
        headers={"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"},
    )
    assert response.status_code == 422
    db_session.refresh(attempt)
    assert attempt.state == S.INITIATED


# --------------------------------------------------------------------------
# Transcript fetch SSRF guard
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/t.json",
        "http://localhost/t.json",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/t.json",
        "http://10.1.2.3/t.json",  # private, not a trusted host
        "http://192.168.0.5/t.json",
        "http://[::1]/t.json",
        "https:///no-host",
    ],
)
def test_transcript_url_to_internal_targets_is_not_fetched(url):
    assert _is_safe_transcript_url(url) is False


def test_public_address_is_allowed():
    assert _is_safe_transcript_url("https://93.184.216.34/t.json") is True


def test_private_address_allowed_only_for_the_dograh_host(monkeypatch):
    monkeypatch.setenv("DOGRAH_API_BASE_URL", "http://10.9.9.9:8000")
    get_settings.cache_clear()
    try:
        assert _is_safe_transcript_url("http://10.9.9.9:9000/t.json") is True
        assert _is_safe_transcript_url("http://10.9.9.10/t.json") is False
        assert _is_safe_transcript_url("http://127.0.0.1/t.json") is False  # never
    finally:
        get_settings.cache_clear()
