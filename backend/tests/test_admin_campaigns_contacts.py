"""Checkpoint 07 §11-14 -- admin campaigns/contacts list/detail,
phone masking, suppression indicator, pagination.
"""

from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.services.phone import normalize_phone_number


def _auth_headers(client):
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_campaign_list_includes_contact_count(client, db_session):
    campaign = Campaign(name="campaign with contacts")
    db_session.add(campaign)
    db_session.flush()
    for i in range(3):
        phone = f"555-900-00{i:02d}"
        db_session.add(
            Contact(
                campaign_id=campaign.id,
                phone_number=phone,
                normalized_phone_number=normalize_phone_number(phone),
            )
        )
    db_session.commit()

    response = client.get("/api/v1/admin/campaigns", headers=_auth_headers(client))

    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["id"] == str(campaign.id))
    assert item["contact_count"] == 3


def test_campaign_detail_metrics(client, db_session):
    campaign = Campaign(name="metrics test")
    db_session.add(campaign)
    db_session.flush()
    phone = "555-900-0100"
    db_session.add(
        Contact(
            campaign_id=campaign.id,
            phone_number=phone,
            normalized_phone_number=normalize_phone_number(phone),
            status=ContactStatus.COMPLETED,
        )
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/admin/campaigns/{campaign.id}", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    assert response.json()["metrics"]["contacts"] == 1
    assert response.json()["metrics"]["completed"] == 1


def test_contact_list_masks_phone_numbers(client, db_session):
    campaign = Campaign(name="masking test")
    db_session.add(campaign)
    db_session.flush()
    phone = "5559001234"
    db_session.add(
        Contact(
            campaign_id=campaign.id,
            phone_number=phone,
            normalized_phone_number=normalize_phone_number(phone),
        )
    )
    db_session.commit()

    response = client.get("/api/v1/admin/contacts", headers=_auth_headers(client))

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["phone_masked"] != normalize_phone_number(phone)
    assert item["phone_masked"].endswith("1234")
    assert "*" in item["phone_masked"]
    assert "phone_number" not in item  # raw field never present on this schema


def test_contact_detail_shows_suppression_indicator(client, db_session):
    campaign = Campaign(name="suppression test")
    db_session.add(campaign)
    db_session.flush()
    phone = "5559005678"
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
    )
    db_session.add(contact)
    db_session.flush()
    db_session.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opted out",
            source=SuppressionSource.AGENT_IN_CALL,
        )
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/admin/contacts/{contact.id}", headers=_auth_headers(client)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["suppressed"] is True
    assert body["suppression_reason"] == "opted out"


def test_contact_without_suppression_shows_false(client, db_session):
    campaign = Campaign(name="no suppression test")
    db_session.add(campaign)
    db_session.flush()
    phone = "5559009999"
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
    )
    db_session.add(contact)
    db_session.commit()

    response = client.get(
        f"/api/v1/admin/contacts/{contact.id}", headers=_auth_headers(client)
    )

    assert response.json()["suppressed"] is False


def test_contacts_and_campaigns_require_auth(client):
    assert client.get("/api/v1/admin/campaigns").status_code == 401
    assert client.get("/api/v1/admin/contacts").status_code == 401
