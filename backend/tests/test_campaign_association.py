"""Campaign/contact association -- Checkpoint 02 Steps 13-16."""

from app.models.enums import SuppressionSource


def _create_campaign(client, name="Assoc campaign", status=None):
    campaign = client.post("/api/v1/campaigns", json={"name": name}).json()
    if status is not None:
        client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": status})
    return campaign["id"]


def _create_contact(client, campaign_id, phone):
    return client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": phone}
    ).json()


def test_associate_contact_moves_it_to_new_campaign(client):
    campaign_a = _create_campaign(client, "A")
    campaign_b = _create_campaign(client, "B")
    contact = _create_contact(client, campaign_a, "555-111-0001")

    response = client.post(f"/api/v1/campaigns/{campaign_b}/contacts/{contact['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["campaign_id"] == campaign_b

    moved = client.get(f"/api/v1/contacts/{contact['id']}").json()
    assert moved["campaign_id"] == campaign_b


def test_associate_with_nonexistent_contact_returns_404(client):
    campaign_id = _create_campaign(client)

    response = client.post(
        f"/api/v1/campaigns/{campaign_id}/contacts/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 404


def test_associate_with_nonexistent_campaign_returns_404(client):
    campaign_a = _create_campaign(client)
    contact = _create_contact(client, campaign_a, "555-111-0002")

    response = client.post(
        f"/api/v1/campaigns/00000000-0000-0000-0000-000000000000/contacts/{contact['id']}"
    )

    assert response.status_code == 404


def test_associate_suppressed_contact_is_rejected(client, db_session):
    from app.models.suppression import Suppression
    from app.services.phone import normalize_phone_number

    campaign_a = _create_campaign(client, "A2")
    campaign_b = _create_campaign(client, "B2")
    contact = _create_contact(client, campaign_a, "555-111-0003")

    db_session.add(
        Suppression(
            contact_id=contact["id"],
            phone_number=normalize_phone_number("555-111-0003"),
            reason="opted out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()

    response = client.post(f"/api/v1/campaigns/{campaign_b}/contacts/{contact['id']}")

    assert response.status_code == 409


def test_associate_deactivated_contact_is_rejected(client):
    campaign_a = _create_campaign(client, "A3")
    campaign_b = _create_campaign(client, "B3")
    contact = _create_contact(client, campaign_a, "555-111-0004")
    client.post(f"/api/v1/contacts/{contact['id']}/deactivate")

    response = client.post(f"/api/v1/campaigns/{campaign_b}/contacts/{contact['id']}")

    assert response.status_code == 409


def test_associate_into_completed_campaign_is_rejected(client):
    campaign_a = _create_campaign(client, "A4")
    campaign_b = _create_campaign(client, "B4")
    client.patch(f"/api/v1/campaigns/{campaign_b}", json={"status": "active"})
    client.patch(f"/api/v1/campaigns/{campaign_b}", json={"status": "completed"})
    contact = _create_contact(client, campaign_a, "555-111-0005")

    response = client.post(f"/api/v1/campaigns/{campaign_b}/contacts/{contact['id']}")

    assert response.status_code == 409


def test_remove_contact_from_campaign_soft_closes_it(client):
    campaign_id = _create_campaign(client)
    contact = _create_contact(client, campaign_id, "555-111-0006")

    response = client.delete(f"/api/v1/campaigns/{campaign_id}/contacts/{contact['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "Closed"

    still_associated = client.get(f"/api/v1/contacts/{contact['id']}").json()
    assert still_associated["campaign_id"] == campaign_id  # history preserved


def test_remove_contact_not_in_that_campaign_returns_404(client):
    campaign_a = _create_campaign(client, "A5")
    campaign_b = _create_campaign(client, "B5")
    contact = _create_contact(client, campaign_a, "555-111-0007")

    response = client.delete(f"/api/v1/campaigns/{campaign_b}/contacts/{contact['id']}")

    assert response.status_code == 404


def test_campaign_contact_counts(client):
    campaign_id = _create_campaign(client)
    _create_contact(client, campaign_id, "555-111-0008")
    contact_2 = _create_contact(client, campaign_id, "555-111-0009")
    client.post(f"/api/v1/contacts/{contact_2['id']}/deactivate")

    response = client.get(f"/api/v1/campaigns/{campaign_id}/contacts/counts")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["eligible"] == 1
    assert body["suppressed"] == 0
