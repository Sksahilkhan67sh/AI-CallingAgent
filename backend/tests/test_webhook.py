"""Telephony call-status webhook -- Checkpoint 03 Step 41."""

from app.core.config import get_settings


def _headers():
    return {"X-Webhook-Secret": get_settings().telephony_webhook_secret}


def _setup_connected_attempt(db_session):
    from app.models.call_attempt import CallAttempt
    from app.models.campaign import Campaign
    from app.models.contact import Contact
    from app.models.enums import CallAttemptState, ContactStatus
    from app.services.phone import normalize_phone_number

    campaign = Campaign(name="Webhook test campaign")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-600-0001",
        normalized_phone_number=normalize_phone_number("555-600-0001"),
        status=ContactStatus.DIALING,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        state=CallAttemptState.INITIATED,
        provider="mock",
        provider_call_id="mock-webhook-call-1",
    )
    db_session.add(attempt)
    db_session.flush()
    return attempt


def test_webhook_rejects_missing_signature(client):
    response = client.post(
        "/api/v1/webhooks/telephony/call-status",
        json={"event_id": "e1", "provider_call_id": "x", "status": "connected"},
    )
    assert response.status_code in (401, 422)  # 422 if header outright missing per FastAPI


def test_webhook_rejects_invalid_signature(client):
    response = client.post(
        "/api/v1/webhooks/telephony/call-status",
        json={"event_id": "e1", "provider_call_id": "x", "status": "connected"},
        headers={"X-Webhook-Secret": "wrong-secret"},
    )
    assert response.status_code == 401


def test_webhook_updates_call_attempt_status(client, db_session):
    attempt = _setup_connected_attempt(db_session)

    response = client.post(
        "/api/v1/webhooks/telephony/call-status",
        json={
            "event_id": "evt-1",
            "provider_call_id": attempt.provider_call_id,
            "status": "connected",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    db_session.refresh(attempt)
    assert attempt.state.value == "Connected"


def test_webhook_is_idempotent_for_duplicate_event_id(client, db_session):
    attempt = _setup_connected_attempt(db_session)
    payload = {
        "event_id": "evt-dup",
        "provider_call_id": attempt.provider_call_id,
        "status": "connected",
    }

    first = client.post("/api/v1/webhooks/telephony/call-status", json=payload, headers=_headers())
    second = client.post("/api/v1/webhooks/telephony/call-status", json=payload, headers=_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_processed"


def test_webhook_for_unknown_call_id_returns_404(client):
    response = client.post(
        "/api/v1/webhooks/telephony/call-status",
        json={"event_id": "evt-2", "provider_call_id": "does-not-exist", "status": "connected"},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_webhook_rejects_update_to_already_terminal_attempt(client, db_session):
    from datetime import UTC, datetime

    attempt = _setup_connected_attempt(db_session)
    attempt.ended_at = datetime.now(UTC)
    db_session.flush()

    response = client.post(
        "/api/v1/webhooks/telephony/call-status",
        json={
            "event_id": "evt-3",
            "provider_call_id": attempt.provider_call_id,
            "status": "failed",
        },
        headers=_headers(),
    )

    assert response.status_code == 409
