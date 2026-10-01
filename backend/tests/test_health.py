def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "service" in body
    assert "environment" in body


def test_readiness_returns_ready_when_dependencies_are_up(client):
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    names = {c["name"] for c in body["components"]}
    assert "PostgreSQL" in names
    assert "Redis" in names
    assert all(c["status"] == "ok" for c in body["components"])


def test_readiness_never_exposes_secrets(client):
    """§9: health responses must not leak internal sensitive data."""
    response = client.get("/ready")
    body_text = response.text.lower()
    assert "password" not in body_text
    assert "secret" not in body_text
    assert "api_key" not in body_text
    assert "dev-only-insecure" not in body_text
