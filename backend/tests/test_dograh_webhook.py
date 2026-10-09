"""Checkpoint 08 -- Dograh post-call webhook: auth, correlation,
classification, idempotency, and hand-off to CP05/CP06's existing
terminal-state machinery.
"""

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.models.retry_policy import RetryPolicy
from tests.phone_helpers import normalize_phone_number


def _connected_call(db_session, *, phone="989-960-0001"):
    campaign = Campaign(name="Dograh webhook test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.IN_CONVERSATION,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider="dograh",
        provider_call_id="99",
        state=CallAttemptState.CONNECTED,
    )
    db_session.add(attempt)
    db_session.flush()
    db_session.commit()
    return campaign, contact, attempt


def _headers():
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


def test_webhook_requires_auth(client, db_session):
    _, _, attempt = _connected_call(db_session)
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "call_status": "user_hangup"},
    )
    assert response.status_code == 401


def test_webhook_rejects_wrong_secret(client, db_session):
    _, _, attempt = _connected_call(db_session, phone="989-960-0002")
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "call_status": "user_hangup"},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401


def test_webhook_accepts_x_api_key_header_too(client, db_session):
    _, _, attempt = _connected_call(db_session, phone="989-960-0003")
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "call_status": "user_hangup"},
        headers={"X-API-Key": get_settings().dograh_webhook_secret},
    )
    assert response.status_code == 200


def test_normal_ending_completes_the_call_and_admits_analysis(client, db_session):
    campaign, contact, attempt = _connected_call(db_session, phone="989-960-0004")
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "call_disposition": "interested",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "ended_normally"

    db_session.refresh(attempt)
    db_session.refresh(contact)
    assert attempt.state == CallAttemptState.ENDED_NORMALLY
    assert contact.status == ContactStatus.COMPLETED
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status.value == "pending"


def test_technical_failure_status_routes_through_recovery(client, db_session):
    campaign, contact, attempt = _connected_call(db_session, phone="989-960-0005")
    # CP14: explicit "no retries" (a missing policy row now means the DEFAULT retries).
    db_session.add(RetryPolicy(campaign_id=campaign.id, max_retries=0, retry_spacing_seconds=[]))
    db_session.commit()
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "call_status": "pipeline_error"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "dropped_mid_call"

    db_session.refresh(attempt)
    assert attempt.state == CallAttemptState.DROPPED_MID_CALL
    assert attempt.disconnect_reason is not None
    # Zero-retry policy -> RecoveryManager terminalizes, same as the native path.
    db_session.refresh(contact)
    assert contact.status == ContactStatus.COMPLETED_PARTIAL


def test_duplicate_delivery_is_idempotent(client, db_session):
    _, _, attempt = _connected_call(db_session, phone="989-960-0006")
    payload = {"call_attempt_id": str(attempt.id), "call_status": "user_hangup"}

    first = client.post(
        "/api/v1/webhooks/dograh/call-completed", json=payload, headers=_headers()
    )
    second = client.post(
        "/api/v1/webhooks/dograh/call-completed", json=payload, headers=_headers()
    )

    assert first.json()["outcome"] == "ended_normally"
    assert second.json()["outcome"] == "already_processed"
    # Only one CallAnalysis row -- the second delivery never re-admits.
    assert (
        db_session.query(CallAnalysis)
        .filter(CallAnalysis.call_attempt_id == attempt.id)
        .count()
        == 1
    )


def test_unknown_call_attempt_returns_404(client, db_session):
    import uuid

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_malformed_call_attempt_id_returns_422(client, db_session):
    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": "not-a-uuid", "call_status": "user_hangup"},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_transcript_fetch_failure_does_not_fail_the_webhook(client, db_session, monkeypatch):
    """§ never fail webhook processing over transcript formatting."""
    _, _, attempt = _connected_call(db_session, phone="989-960-0007")

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={
            "call_attempt_id": str(attempt.id),
            "call_status": "user_hangup",
            "transcript_url": "https://example.invalid/not-real",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "ended_normally"
