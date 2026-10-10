"""Checkpoint 09 -- Dograh webhook hardening: three-way classification
(including the newly-added never-connected branch), ProcessedEvent-
based replay protection, and payload validation.
"""

import pytest

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.processed_event import ProcessedEvent
from app.schemas.dograh_webhook import DograhWebhookPayload
from tests.phone_helpers import normalize_phone_number


def _initiated_call(db_session, *, phone="989-970-0001"):
    """Checkpoint 09 §2: a triggered-but-not-yet-resolved Dograh call
    sits at CallAttempt's default INITIATED state, not CONNECTED --
    this fixture matches that corrected reality."""
    campaign = Campaign(name="Dograh webhook hardening test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DIALING,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider="dograh",
        provider_call_id="99",
        state=CallAttemptState.INITIATED,
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.commit()
    return campaign, contact, attempt


def _headers():
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


def test_never_answered_status_is_classified_as_never_connected(client, db_session):
    """§2/§3: the webhook is the only place this integration learns
    whether a call actually connected -- 'no_answer' must route through
    the never-connected recovery path, not be treated as a completed
    conversation."""
    _, contact, attempt = _initiated_call(db_session, phone="989-970-0002")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "workflow_run_id": 99,
            "call_status": "no_answer",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "never_connected"

    db_session.refresh(attempt)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
    assert attempt.connection_failure_reason is not None
    assert attempt.connection_failure_reason.value == "no_answer"


def test_busy_status_maps_to_busy_reason(client, db_session):
    _, _, attempt = _initiated_call(db_session, phone="989-970-0003")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "workflow_run_id": 99, "call_status": "busy"},
        headers=_headers(),
    )

    assert response.json()["outcome"] == "never_connected"
    db_session.refresh(attempt)
    assert attempt.connection_failure_reason.value == "busy"


def test_never_connected_call_gets_no_conversation_session(client, db_session):
    """A call that never connected has no conversation -- no
    ConversationSession should be created for it."""
    from app.models.conversation import ConversationSession

    _, _, attempt = _initiated_call(db_session, phone="989-970-0004")

    client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "workflow_run_id": 99,
            "call_status": "no_answer",
        },
        headers=_headers(),
    )

    assert (
        db_session.query(ConversationSession)
        .filter(ConversationSession.call_attempt_id == attempt.id)
        .count()
        == 0
    )


def test_duplicate_workflow_run_id_is_a_processed_event_noop(client, db_session):
    """§3.3/§3.4: replay protection keyed on workflow_run_id (Dograh's
    own event identity), reusing the existing ProcessedEvent model --
    a genuinely duplicate delivery is a no-op even before the
    attempt-state check runs."""
    _, _, attempt = _initiated_call(db_session, phone="989-970-0005")
    payload = {
        "call_attempt_id": str(attempt.id),
        "workflow_run_id": 99,
        "call_status": "user_hangup",
    }

    first = client.post(
        "/api/v1/webhooks/dograh/call-completed", json=payload, headers=_headers()
    )
    second = client.post(
        "/api/v1/webhooks/dograh/call-completed", json=payload, headers=_headers()
    )

    assert first.json()["outcome"] == "ended_normally"
    assert second.json()["outcome"] == "already_processed"
    assert (
        db_session.query(ProcessedEvent)
        .filter(ProcessedEvent.event_id == "dograh:99")
        .count()
        == 1
    )


def test_missing_workflow_run_id_falls_back_to_call_attempt_id_for_event_identity(
    client, db_session
):
    _, _, attempt = _initiated_call(db_session, phone="989-970-0006")
    payload = {"call_attempt_id": str(attempt.id), "call_status": "user_hangup"}

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed", json=payload, headers=_headers()
    )

    assert response.status_code == 200
    assert (
        db_session.query(ProcessedEvent)
        .filter(ProcessedEvent.event_id == f"dograh:{attempt.id}")
        .count()
        == 1
    )


# --------------------------------------------------------------------------
# CP15 prep: `recording_url` is advisory -- it must never reject an otherwise valid webhook, and
# it must never be fetched or trusted. Every other validation stays strict.
# --------------------------------------------------------------------------
_RECORDING_URL = "https://dograh.example.invalid/api/v1/public/download/workflow/tok-123/recording"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_RECORDING_URL, _RECORDING_URL),  # documented: public download URL (token branch)
        ("http://dograh.example.invalid/x", "http://dograh.example.invalid/x"),
        ("recordings/123.wav", None),  # documented: bare storage key (no-token branch)
        ("recordings/123/user.wav", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("ftp://example.invalid/a.wav", None),
        ("", None),
        ("   ", None),
        ("https://example.invalid/a b.wav", None),  # whitespace inside
        ("https://example.invalid/a\nb.wav", None),  # control character
        ("https://example.invalid/" + "a" * 3000, None),  # over the length bound
        (12345, None),
        (True, None),
        (["https://example.invalid/a.wav"], None),
        ({"url": "https://example.invalid/a.wav"}, None),
    ],
)
def test_recording_url_is_normalised_never_rejected(raw, expected):
    payload = DograhWebhookPayload.model_validate(
        {"call_attempt_id": "abc", "recording_url": raw}
    )
    assert payload.recording_url == expected


def test_recording_url_absent_or_null_is_none():
    assert DograhWebhookPayload.model_validate({"call_attempt_id": "abc"}).recording_url is None
    assert (
        DograhWebhookPayload.model_validate(
            {"call_attempt_id": "abc", "recording_url": None}
        ).recording_url
        is None
    )


@pytest.mark.parametrize(
    "recording_url",
    ["recordings/123.wav", "file:///etc/passwd", 42, "x" * 5000],
    ids=["bare-storage-key", "file-scheme", "non-string", "oversized"],
)
def test_malformed_recording_url_does_not_reject_a_valid_webhook(
    client, db_session, monkeypatch, recording_url
):
    """A bad recording field cannot lose the call result, and nothing is fetched because of it."""
    import app.services.telephony.dograh_webhook_service as svc

    def _no_fetch(*_a, **_k):  # pragma: no cover - failing here is the assertion
        raise AssertionError("recording_url must never trigger a fetch")

    monkeypatch.setattr(svc, "fetch_transcript_lines", _no_fetch)
    _, _, attempt = _initiated_call(db_session, phone="989-970-0101")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "recording_url": recording_url,
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "ended_normally"
    assert (
        db_session.query(ProcessedEvent)
        .filter(ProcessedEvent.event_id == f"dograh:{attempt.id}")
        .count()
        == 1
    )


def test_malformed_recording_url_does_not_bypass_authentication(client, db_session):
    _, _, attempt = _initiated_call(db_session, phone="989-970-0102")
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "recording_url": "recordings/123.wav",
        },
    )
    assert response.status_code == 401
    db_session.refresh(attempt)
    assert attempt.state == CallAttemptState.INITIATED


def test_malformed_recording_url_does_not_relax_other_validation(client, db_session):
    """A tolerated recording field must not mask a genuinely invalid payload."""
    import uuid

    for extra in (
        {"call_status": "x" * 501},  # bounded free text
        {"call_status": "user_hangup", "transcript_url": "file:///etc/passwd"},  # fetched URL
    ):
        response = client.post(
            "/api/v1/webhooks/dograh/call-completed",
            json={
                "call_attempt_id": str(uuid.uuid4()),
                "recording_url": "recordings/123.wav",
                **extra,
            },
            headers=_headers(),
        )
        assert response.status_code == 422, extra
    too_long_id = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": "x" * 100,
            "call_status": "user_hangup",
            "recording_url": "recordings/123.wav",
        },
        headers=_headers(),
    )
    assert too_long_id.status_code == 422


def test_transcript_url_remains_strictly_validated():
    with pytest.raises(ValueError):
        DograhWebhookPayload.model_validate(
            {"call_attempt_id": "abc", "transcript_url": "transcripts/123.txt"}
        )


def test_payload_rejects_call_attempt_id_too_long(client, db_session):
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": "x" * 100, "call_status": "user_hangup"},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_payload_rejects_oversized_call_status(client, db_session):
    import uuid

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(uuid.uuid4()), "call_status": "x" * 501},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_payload_tolerates_unknown_extra_fields(client, db_session):
    """Dograh's own payload_template is user-configurable and may grow
    new variables over time -- this integration must not hard-fail on
    fields it doesn't recognize yet."""
    _, _, attempt = _initiated_call(db_session, phone="989-970-0008")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "some_future_field": "unexpected",
        },
        headers=_headers(),
    )
    assert response.status_code == 200


def test_schema_directly_rejects_empty_call_attempt_id():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DograhWebhookPayload(call_attempt_id="")
