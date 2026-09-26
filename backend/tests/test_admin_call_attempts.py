"""Checkpoint 07 §15-17, §21, §36 -- call attempts list/detail, IDOR,
and 404 handling.
"""

import uuid

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import (
    CallEvent,
    ConversationMessage,
    ConversationRole,
    ConversationSession,
)
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus, InterestStatus
from app.services.phone import normalize_phone_number


def _auth_headers(client):
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _full_call(db_session, *, phone="555-800-0001"):
    campaign = Campaign(name="call detail test")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        ConversationMessage(
            session_id=session.id, sequence=1, role=ConversationRole.AGENT, content="Hi there"
        )
    )
    db_session.add(
        CallEvent(call_attempt_id=attempt.id, event_type="RECOVERY_SCHEDULED", payload={})
    )
    db_session.add(
        CallEvent(call_attempt_id=attempt.id, event_type="ANALYSIS_COMPLETED", payload={})
    )
    db_session.add(
        CallAnalysis(
            call_attempt_id=attempt.id,
            contact_id=contact.id,
            campaign_id=campaign.id,
            status=AnalysisStatus.COMPLETED,
            interest_status=InterestStatus.INTERESTED,
            lead_score=75,
            summary="test summary",
        )
    )
    db_session.commit()
    return campaign, contact, attempt


def test_list_requires_auth(client):
    response = client.get("/api/v1/admin/call-attempts")
    assert response.status_code == 401


def test_list_returns_call_attempts_with_analysis_joined(client, db_session):
    _, _, attempt = _full_call(db_session)

    response = client.get("/api/v1/admin/call-attempts", headers=_auth_headers(client))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(attempt.id)
    assert item["analysis_status"] == "completed"
    assert item["lead_score"] == 75


def test_list_filters_by_campaign(client, db_session):
    campaign_a, _, attempt_a = _full_call(db_session, phone="555-800-0002")
    campaign_b, _, attempt_b = _full_call(db_session, phone="555-800-0003")

    response = client.get(
        "/api/v1/admin/call-attempts",
        params={"campaign_id": str(campaign_a.id)},
        headers=_auth_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(attempt_a.id)


def test_detail_includes_transcript_analysis_and_recovery_events(client, db_session):
    _, contact, attempt = _full_call(db_session, phone="555-800-0004")

    response = client.get(
        f"/api/v1/admin/call-attempts/{attempt.id}", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contact_phone_masked"] != contact.phone_number  # never the raw number
    assert body["contact_phone_masked"].endswith("0004")
    assert "*" in body["contact_phone_masked"]
    assert len(body["transcript"]) == 1
    assert body["transcript"][0]["content"] == "Hi there"
    assert body["analysis"]["summary"] == "test summary"
    assert body["analysis"]["lead_score"] == 75
    recovery_types = [e["event_type"] for e in body["recovery_events"]]
    assert "RECOVERY_SCHEDULED" in recovery_types
    assert "ANALYSIS_COMPLETED" not in recovery_types  # not a recovery event


def test_detail_404_for_missing_call_attempt(client, db_session):
    response = client.get(
        f"/api/v1/admin/call-attempts/{uuid.uuid4()}", headers=_auth_headers(client)
    )
    assert response.status_code == 404


def test_changing_id_in_url_does_not_leak_a_different_calls_data(client, db_session):
    """§36 IDOR check: two different call attempts' detail responses
    must never cross-contaminate."""
    _, _, attempt_a = _full_call(db_session, phone="555-800-0005")
    _, _, attempt_b = _full_call(db_session, phone="555-800-0006")

    resp_a = client.get(
        f"/api/v1/admin/call-attempts/{attempt_a.id}", headers=_auth_headers(client)
    ).json()
    resp_b = client.get(
        f"/api/v1/admin/call-attempts/{attempt_b.id}", headers=_auth_headers(client)
    ).json()

    assert resp_a["id"] == str(attempt_a.id)
    assert resp_b["id"] == str(attempt_b.id)
    assert resp_a["contact_phone_masked"] != resp_b["contact_phone_masked"]
