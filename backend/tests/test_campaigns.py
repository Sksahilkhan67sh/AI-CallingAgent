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
