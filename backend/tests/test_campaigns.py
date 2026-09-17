def test_create_campaign(client):
    response = client.post("/api/v1/campaigns", json={"name": "Q1 Outreach"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Q1 Outreach"
    assert body["status"] == "draft"
    assert "id" in body


def test_get_campaign(client):
    created = client.post("/api/v1/campaigns", json={"name": "Q2 Outreach"}).json()

    response = client.get(f"/api/v1/campaigns/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_campaign_returns_404(client):
    response = client.get("/api/v1/campaigns/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_list_campaigns_is_paginated(client):
    for i in range(3):
        client.post("/api/v1/campaigns", json={"name": f"Paginated {i}"})

    response = client.get("/api/v1/campaigns?limit=2&offset=0")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 3
    assert body["limit"] == 2
    assert body["offset"] == 0


def test_list_campaigns_filters_by_status(client):
    campaign = client.post("/api/v1/campaigns", json={"name": "Filter test"}).json()
    client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "active"})

    response = client.get("/api/v1/campaigns?status=active")

    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["items"]]
    assert campaign["id"] in ids


def test_update_campaign_name(client):
    campaign = client.post("/api/v1/campaigns", json={"name": "Old name"}).json()

    response = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"name": "New name"})

    assert response.status_code == 200
    assert response.json()["name"] == "New name"


def test_valid_status_transition(client):
    campaign = client.post("/api/v1/campaigns", json={"name": "Transition test"}).json()

    response = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "active"})

    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_invalid_status_transition_is_rejected(client):
    campaign = client.post("/api/v1/campaigns", json={"name": "Bad transition test"}).json()
    client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "active"})
    client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "completed"})

    # completed is terminal -- cannot go back to active
    response = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"status": "active"})

    assert response.status_code == 422


def test_update_unknown_campaign_returns_404(client):
    response = client.patch(
        "/api/v1/campaigns/00000000-0000-0000-0000-000000000000", json={"name": "x"}
    )

    assert response.status_code == 404
