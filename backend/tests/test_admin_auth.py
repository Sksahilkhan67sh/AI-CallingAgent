"""Checkpoint 07 §4-5, §36 -- admin dashboard authentication/authorization."""

from app.core.config import get_settings


def test_login_with_admin_credentials_succeeds(client):
    settings = get_settings()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "admin"
    assert body["access_token"]
    assert body["token_type"] == "bearer"


def test_login_with_operator_credentials_succeeds(client):
    settings = get_settings()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.operator_username, "password": settings.operator_password},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "operator"


def test_login_with_wrong_password_is_rejected(client):
    settings = get_settings()
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_login_with_unknown_username_is_rejected(client):
    response = client.post(
        "/api/v1/admin/auth/login",
        json={"username": "nobody", "password": "whatever"},
    )
    assert response.status_code == 401


def test_dashboard_overview_requires_authentication(client):
    response = client.get("/api/v1/admin/dashboard/overview")
    assert response.status_code == 401


def test_dashboard_overview_rejects_malformed_token(client):
    response = client.get(
        "/api/v1/admin/dashboard/overview", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_dashboard_overview_rejects_non_bearer_scheme(client):
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    token = login.json()["access_token"]
    response = client.get(
        "/api/v1/admin/dashboard/overview", headers={"Authorization": f"Token {token}"}
    )
    assert response.status_code == 401


def test_dashboard_overview_succeeds_with_valid_admin_token(client):
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    token = login.json()["access_token"]
    response = client.get(
        "/api/v1/admin/dashboard/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_dashboard_overview_succeeds_with_valid_operator_token(client):
    """Operators can read monitoring data (§5)."""
    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.operator_username, "password": settings.operator_password},
    )
    token = login.json()["access_token"]
    response = client.get(
        "/api/v1/admin/dashboard/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_campaign_status_transition_requires_admin_role(client):
    """§5, §38: operational controls require the admin role -- an
    operator token must be rejected even though it's a valid token."""
    import uuid

    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.operator_username, "password": settings.operator_password},
    )
    token = login.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/campaigns/{uuid.uuid4()}/status",
        params={"new_status": "active"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_campaign_status_transition_allows_admin_role(client, db_session):

    from app.models.campaign import Campaign

    campaign = Campaign(name="role test")
    db_session.add(campaign)
    db_session.commit()

    settings = get_settings()
    login = client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    token = login.json()["access_token"]

    response = client.post(
        f"/api/v1/admin/campaigns/{campaign.id}/status",
        params={"new_status": "active"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"
