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
from app.services.phone import normalize_phone_number


def _initiated_call(db_session, *, phone="555-970-0001"):
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
    _, contact, attempt = _initiated_call(db_session, phone="555-970-0002")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "workflow_run_id": 1001,
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
    _, _, attempt = _initiated_call(db_session, phone="555-970-0003")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "workflow_run_id": 1002, "call_status": "busy"},
        headers=_headers(),
    )

    assert response.json()["outcome"] == "never_connected"
    db_session.refresh(attempt)
    assert attempt.connection_failure_reason.value == "busy"


def test_never_connected_call_gets_no_conversation_session(client, db_session):
    """A call that never connected has no conversation -- no
    ConversationSession should be created for it."""
    from app.models.conversation import ConversationSession

    _, _, attempt = _initiated_call(db_session, phone="555-970-0004")

    client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "workflow_run_id": 1003,
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
    _, _, attempt = _initiated_call(db_session, phone="555-970-0005")
    payload = {
        "call_attempt_id": str(attempt.id),
        "workflow_run_id": 2001,
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
        .filter(ProcessedEvent.event_id == "dograh:2001")
        .count()
        == 1
    )


def test_missing_workflow_run_id_falls_back_to_call_attempt_id_for_event_identity(
    client, db_session
):
    _, _, attempt = _initiated_call(db_session, phone="555-970-0006")
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


def test_payload_rejects_non_http_recording_url(client, db_session):
    _, _, attempt = _initiated_call(db_session, phone="555-970-0007")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "recording_url": "file:///etc/passwd",
        },
        headers=_headers(),
    )

    assert response.status_code == 422


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
    _, _, attempt = _initiated_call(db_session, phone="555-970-0008")

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
