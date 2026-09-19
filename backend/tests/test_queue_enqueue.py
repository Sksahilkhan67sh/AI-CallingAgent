"""Campaign enqueue endpoint -- Checkpoint 03 Steps 24-26."""

from app.models.enums import SuppressionSource


def _create_active_campaign(client, name="Enqueue campaign"):
    campaign = client.post("/api/v1/campaigns", json={"name": name}).json()
    client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "active"})
    return campaign["id"]


def test_enqueue_eligible_contact(client, redis_client):
    campaign_id = _create_active_campaign(client)
    client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "555-400-0001"}
    )

    response = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue")

    assert response.status_code == 200
    body = response.json()
    assert body["enqueued"] == 1
    assert body["skipped_suppressed"] == 0
    assert redis_client.xlen("calls:outbound") == 1


def test_enqueue_suppressed_contact_is_skipped(client, redis_client, db_session):
    from app.models.suppression import Suppression
    from app.services.phone import normalize_phone_number

    campaign_id = _create_active_campaign(client, "Suppressed enqueue")
    contact = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "555-400-0002"}
    ).json()
    db_session.add(
        Suppression(
            contact_id=contact["id"],
            phone_number=normalize_phone_number("555-400-0002"),
            reason="opted out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()

    response = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue")

    assert response.status_code == 200
    body = response.json()
    assert body["enqueued"] == 0
    assert body["skipped_suppressed"] == 1


def test_enqueue_inactive_campaign_is_rejected(client, redis_client):
    campaign = client.post("/api/v1/campaigns", json={"name": "Draft campaign"}).json()

    response = client.post(f"/api/v1/campaigns/{campaign['id']}/enqueue")

    assert response.status_code == 422


def test_enqueue_nonexistent_campaign_returns_404(client, redis_client):
    response = client.post(
        "/api/v1/campaigns/00000000-0000-0000-0000-000000000000/enqueue"
    )

    assert response.status_code == 404


def test_duplicate_enqueue_does_not_double_queue(client, redis_client):
    campaign_id = _create_active_campaign(client, "Double enqueue")
    client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "555-400-0003"}
    )

    first = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue").json()
    second = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue").json()

    assert first["enqueued"] == 1
    assert second["enqueued"] == 0
    assert second["skipped_duplicate"] == 1
    assert redis_client.xlen("calls:outbound") == 1


def test_enqueue_does_not_target_closed_contacts(client, redis_client):
    campaign_id = _create_active_campaign(client, "Skip closed contacts")
    contact = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign_id, "phone_number": "555-400-0004"}
    ).json()
    client.post(f"/api/v1/contacts/{contact['id']}/deactivate")  # status -> Closed

    response = client.post(f"/api/v1/campaigns/{campaign_id}/enqueue").json()

    assert response["enqueued"] == 0
