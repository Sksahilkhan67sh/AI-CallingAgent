"""Checkpoint 07 §9-10, §45-46 -- dashboard overview aggregate
correctness, with explicit contact-count != attempt-count coverage.
"""

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import (
    AnalysisStatus,
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    InterestStatus,
)
from app.services.phone import normalize_phone_number


def _auth_headers(client):
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_no_data_returns_zeros_not_errors(client, db_session):
    response = client.get("/api/v1/admin/dashboard/overview", headers=_auth_headers(client))
    assert response.status_code == 200
    body = response.json()
    assert body["calls"]["total"] == 0
    assert body["intelligence"]["average_lead_score"] is None


def test_a_contact_with_two_attempts_counts_as_one_contact_two_attempts(client, db_session):
    """§45: Contact A, attempt 1 failed, attempt 2 completed ->
    contacts = 1, attempts = 2 -- never accidentally collapsed."""
    campaign = Campaign(name="dashboard aggregate test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    phone = "555-700-0001"
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.COMPLETED,
        attempt_count=2,
    )
    db_session.add(contact)
    db_session.flush()
    db_session.add_all(
        [
            CallAttempt(
                contact_id=contact.id,
                attempt_number=1,
                state=CallAttemptState.FAILED_TO_CONNECT,
            ),
            CallAttempt(
                contact_id=contact.id,
                attempt_number=2,
                state=CallAttemptState.ENDED_NORMALLY,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/v1/admin/dashboard/overview", headers=_auth_headers(client))
    body = response.json()

    assert body["calls"]["total"] == 1  # one Contact
    # attempts aren't in CallOverview directly, but reliability uses
    # CallAttempt counts -- verify via the analytics endpoint instead,
    # which explicitly reports total_dial_attempts.
    analytics = client.get(
        "/api/v1/admin/dashboard/analytics?range=30d", headers=_auth_headers(client)
    ).json()
    assert analytics["total_dial_attempts"] == 2


def test_mixed_outcomes_produce_correct_intelligence_counts(client, db_session):
    campaign = Campaign(name="intelligence aggregate test")
    db_session.add(campaign)
    db_session.flush()

    interest_values = [
        InterestStatus.INTERESTED,
        InterestStatus.INTERESTED,
        InterestStatus.MAYBE,
        InterestStatus.NOT_INTERESTED,
        InterestStatus.UNKNOWN,
    ]
    scores = [80, 60, 40, 10, 0]
    for i, (interest, score) in enumerate(zip(interest_values, scores, strict=True)):
        phone = f"555-700-01{i:02d}"
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
        db_session.add(
            CallAnalysis(
                call_attempt_id=attempt.id,
                contact_id=contact.id,
                campaign_id=campaign.id,
                status=AnalysisStatus.COMPLETED,
                interest_status=interest,
                lead_score=score,
            )
        )
    db_session.commit()

    response = client.get("/api/v1/admin/dashboard/overview", headers=_auth_headers(client))
    body = response.json()["intelligence"]

    assert body["analyzed"] == 5
    assert body["interested"] == 2
    assert body["maybe"] == 1
    assert body["not_interested"] == 1
    assert body["unknown"] == 1
    assert body["average_lead_score"] == sum(scores) / len(scores)


def test_all_failures_produce_full_failure_rate(client, db_session):
    campaign = Campaign(name="all failures test")
    db_session.add(campaign)
    db_session.flush()
    for i in range(3):
        phone = f"555-700-02{i:02d}"
        contact = Contact(
            campaign_id=campaign.id,
            phone_number=phone,
            normalized_phone_number=normalize_phone_number(phone),
            status=ContactStatus.CLOSED,
        )
        db_session.add(contact)
        db_session.flush()
        db_session.add(
            CallAttempt(
                contact_id=contact.id,
                attempt_number=1,
                state=CallAttemptState.FAILED_TO_CONNECT,
            )
        )
    db_session.commit()

    response = client.get("/api/v1/admin/dashboard/overview", headers=_auth_headers(client))
    body = response.json()["reliability"]
    assert body["failure_rate"] == 1.0
