"""Checkpoint 06 §37 -- read-only analysis API endpoints."""

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import (
    AnalysisIntent,
    AnalysisSentiment,
    AnalysisStatus,
    CallAttemptState,
    ContactStatus,
    InterestStatus,
)
from app.services.phone import normalize_phone_number


def _analysis(db_session, *, phone="555-400-0001", status=AnalysisStatus.COMPLETED):
    campaign = Campaign(name="CP06 api test")
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
    analysis = CallAnalysis(
        call_attempt_id=attempt.id,
        contact_id=contact.id,
        campaign_id=campaign.id,
        status=status,
        summary="Customer was interested." if status == AnalysisStatus.COMPLETED else None,
        intent=AnalysisIntent.INTERESTED if status == AnalysisStatus.COMPLETED else None,
        interest_status=(
            InterestStatus.INTERESTED if status == AnalysisStatus.COMPLETED else None
        ),
        sentiment=AnalysisSentiment.POSITIVE if status == AnalysisStatus.COMPLETED else None,
        lead_score=70 if status == AnalysisStatus.COMPLETED else None,
    )
    db_session.add(analysis)
    db_session.flush()
    db_session.commit()
    return campaign, contact, attempt, analysis


def test_get_analysis_by_call_attempt(client, db_session):
    _, _, attempt, analysis = _analysis(db_session)

    response = client.get(f"/api/v1/call-attempts/{attempt.id}/analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(analysis.id)
    assert body["summary"] == "Customer was interested."
    assert body["lead_score"] == 70


def test_get_analysis_by_call_attempt_404_when_missing(client, db_session):
    import uuid

    response = client.get(f"/api/v1/call-attempts/{uuid.uuid4()}/analysis")

    assert response.status_code == 404


def test_get_latest_analysis_by_contact(client, db_session):
    _, contact, _, analysis = _analysis(db_session, phone="555-400-0002")

    response = client.get(f"/api/v1/contacts/{contact.id}/analysis")

    assert response.status_code == 200
    assert response.json()["id"] == str(analysis.id)


def test_list_analysis_by_campaign_is_paginated(client, db_session):
    campaign, _, _, _ = _analysis(db_session, phone="555-400-0003")

    response = client.get(f"/api/v1/campaigns/{campaign.id}/analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_pending_analysis_exposes_status_without_results(client, db_session):
    _, _, attempt, analysis = _analysis(
        db_session, phone="555-400-0004", status=AnalysisStatus.PENDING
    )

    response = client.get(f"/api/v1/call-attempts/{attempt.id}/analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["summary"] is None
    assert body["lead_score"] is None
