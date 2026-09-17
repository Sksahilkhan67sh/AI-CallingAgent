def _create_campaign(client) -> str:
    return client.post("/api/v1/campaigns", json={"name": "Test Campaign"}).json()["id"]


def test_create_contact(client):
    campaign_id = _create_campaign(client)

    response = client.post(
        "/api/v1/contacts",
        json={"campaign_id": campaign_id, "phone_number": "(555) 123-4567"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["campaign_id"] == campaign_id
    assert body["normalized_phone_number"] == "+15551234567"
    assert body["status"] == "Pending"
    assert body["attempt_count"] == 0


def test_get_contact(client):
    campaign_id = _create_campaign(client)
    created = client.post(
        "/api/v1/contacts",
        json={"campaign_id": campaign_id, "phone_number": "555-987-6543"},
    ).json()

    response = client.get(f"/api/v1/contacts/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_contact_returns_404(client):
    response = client.get("/api/v1/contacts/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_duplicate_contact_in_same_campaign_is_rejected(client):
    campaign_id = _create_campaign(client)
    payload = {"campaign_id": campaign_id, "phone_number": "555-111-2222"}
    client.post("/api/v1/contacts", json=payload)

    # same number, different formatting -- must still collide on the
    # normalized representation
    response = client.post(
        "/api/v1/contacts",
        json={"campaign_id": campaign_id, "phone_number": "(555) 111-2222"},
    )

    assert response.status_code == 409


def test_same_number_allowed_in_different_campaigns(client):
    campaign_a = _create_campaign(client)
    campaign_b = _create_campaign(client)
    phone = "555-333-4444"

    first = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_a, "phone_number": phone}
    )
    second = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_b, "phone_number": phone}
    )

    assert first.status_code == 201
    assert second.status_code == 201


def test_invalid_phone_number_is_rejected(client):
    campaign_id = _create_campaign(client)

    response = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "abc"}
    )

    assert response.status_code == 422


def test_contact_for_unknown_campaign_returns_404(client):
    response = client.post(
        "/api/v1/contacts",
        json={
            "campaign_id": "00000000-0000-0000-0000-000000000000",
            "phone_number": "555-999-1111",
        },
    )

    assert response.status_code == 404


def test_suppressed_number_cannot_be_added_as_contact(client, db_session):

    from app.models.campaign import Campaign
    from app.models.contact import Contact
    from app.models.enums import SuppressionSource
    from app.models.suppression import Suppression
    from app.services.phone import normalize_phone_number

    campaign = Campaign(name="Suppression test campaign")
    db_session.add(campaign)
    db_session.flush()

    # Suppression references a contact_id (Database Design §2.12); use a
    # placeholder contact to represent "already opted out", matching the
    # spec's "caught even under a new contact_id" note -- the check here
    # is by phone number, not by this placeholder's own id.
    placeholder_contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-777-8888",
        normalized_phone_number=normalize_phone_number("555-777-8888"),
    )
    db_session.add(placeholder_contact)
    db_session.flush()

    db_session.add(
        Suppression(
            contact_id=placeholder_contact.id,
            phone_number=normalize_phone_number("555-777-8888"),
            reason="requested opt-out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()

    other_campaign_id = _create_campaign(client)
    response = client.post(
        "/api/v1/contacts",
        json={"campaign_id": other_campaign_id, "phone_number": "555-777-8888"},
    )

    assert response.status_code == 409
