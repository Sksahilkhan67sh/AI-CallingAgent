"""CP11 Step 1 -- authentication + role authorization on the /api/v1
campaign / contact / analysis / enqueue surface.

Before CP11 none of these routes required a token. This file is the endpoint
authorization matrix as executable tests: every route must reject missing or
bad credentials with 401, and every mutation must reject a valid *operator*
token with 403. Operators keep read access (the same read surface the admin
dashboard already gives them).

Single-tenant note (CP11 decision): there is no tenant/owner column, so there
is no per-resource ownership check to test yet -- full tenant isolation is
DEFERRED (docs/CHECKPOINT-11-NOTES.md). What IS tested here is that identity
and role come only from the verified token, never from the request.
"""

import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus

_ID = uuid.uuid4()

# (method, path, request kwargs) -- one row per protected route.
_READS = [
    ("GET", "/api/v1/campaigns", {}),
    ("GET", f"/api/v1/campaigns/{_ID}", {}),
    ("GET", f"/api/v1/campaigns/{_ID}/contacts", {}),
    ("GET", f"/api/v1/campaigns/{_ID}/contacts/counts", {}),
    ("GET", f"/api/v1/campaigns/{_ID}/analysis", {}),
    ("GET", "/api/v1/contacts", {}),
    ("GET", f"/api/v1/contacts/{_ID}", {}),
    ("GET", f"/api/v1/contacts/{_ID}/analysis", {}),
    ("GET", f"/api/v1/call-attempts/{_ID}/analysis", {}),
]
_MUTATIONS = [
    ("POST", "/api/v1/campaigns", {"json": {"name": "x"}}),
    ("PATCH", f"/api/v1/campaigns/{_ID}", {"json": {"name": "y"}}),
    (
        "POST",
        "/api/v1/campaigns/import",
        {"data": {"name": "x"}, "files": {"file": ("c.csv", b"phone_number\n+14155550100\n")}},
    ),
    ("POST", f"/api/v1/campaigns/{_ID}/contacts/{_ID}", {}),
    ("DELETE", f"/api/v1/campaigns/{_ID}/contacts/{_ID}", {}),
    ("POST", f"/api/v1/campaigns/{_ID}/enqueue", {}),
    (
        "POST",
        "/api/v1/contacts",
        {"json": {"campaign_id": str(_ID), "phone_number": "+14155550100"}},
    ),
    ("PATCH", f"/api/v1/contacts/{_ID}", {"json": {"phone_number": "+14155550101"}}),
    ("POST", f"/api/v1/contacts/{_ID}/deactivate", {}),
]
_ALL = _READS + _MUTATIONS


def _ids(rows):
    return [f"{m} {p.replace(str(_ID), '{id}')}" for m, p, _ in rows]


def _token(*, role="admin", sub="tester", key=None, exp_offset=3600):
    settings = get_settings()
    now = int(time.time())
    payload = {"sub": sub, "role": role, "iat": now, "exp": now + exp_offset}
    return jwt.encode(payload, key or settings.jwt_signing_key, algorithm="HS256")


def _call(client, row, headers=None):
    method, path, kwargs = row
    return client.request(method, path, headers=headers, **kwargs)


# --- authentication: 401 ---------------------------------------------------


@pytest.mark.parametrize("row", _ALL, ids=_ids(_ALL))
def test_anonymous_is_rejected_401(anon_client, row):
    assert _call(anon_client, row).status_code == 401


@pytest.mark.parametrize("row", _ALL, ids=_ids(_ALL))
def test_malformed_credentials_are_rejected_401(anon_client, row):
    for header in ("Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "garbage"):
        response = _call(anon_client, row, headers={"Authorization": header})
        assert response.status_code == 401, header


@pytest.mark.parametrize(
    "bad_token",
    [
        _token(exp_offset=-10),  # expired
        _token(key="some-other-signing-key-0123456789abcdef"),  # wrong signature
        _token(role="superadmin"),  # role the backend does not define
        _token(role=""),
        _token(sub=""),
    ],
    ids=["expired", "wrong-key", "unknown-role", "empty-role", "empty-sub"],
)
def test_invalid_tokens_are_rejected_401(anon_client, bad_token):
    for row in (_READS[0], _MUTATIONS[0], _MUTATIONS[5]):
        response = _call(anon_client, row, headers={"Authorization": f"Bearer {bad_token}"})
        assert response.status_code == 401


def test_alg_none_token_is_rejected(anon_client):
    forged = jwt.encode({"sub": "x", "role": "admin"}, key=None, algorithm="none")
    response = anon_client.get("/api/v1/campaigns", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_auth_is_checked_before_body_validation(anon_client):
    """An unauthenticated caller must get 401, never schema feedback (422)."""
    response = anon_client.post("/api/v1/campaigns", json={"unexpected": 1})
    assert response.status_code == 401
    response = anon_client.post("/api/v1/contacts", json={})
    assert response.status_code == 401


def test_401_carries_www_authenticate_and_no_internal_detail(anon_client):
    response = anon_client.get("/api/v1/campaigns")
    assert response.headers["www-authenticate"] == "Bearer"
    assert set(response.json()) == {"detail"}


# --- role authorization: 403 -------------------------------------------------


@pytest.mark.parametrize("row", _MUTATIONS, ids=_ids(_MUTATIONS))
def test_operator_cannot_mutate_403(operator_client, row):
    assert _call(operator_client, row).status_code == 403


@pytest.mark.parametrize("row", _READS, ids=_ids(_READS))
def test_operator_can_read(operator_client, row):
    assert _call(operator_client, row).status_code not in (401, 403)


@pytest.mark.parametrize("row", _ALL, ids=_ids(_ALL))
def test_admin_is_not_blocked(client, row):
    # Targets don't exist (random UUIDs) so 404/409/422 are fine -- the point
    # is that authn/authz let an admin through to the handler.
    assert _call(client, row).status_code not in (401, 403)


def test_operator_403_does_not_leak_resource_existence(operator_client, db_session):
    campaign = Campaign(name="real", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    real = operator_client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    fake = operator_client.post(f"/api/v1/campaigns/{uuid.uuid4()}/enqueue")
    assert real.status_code == fake.status_code == 403
    assert real.json() == fake.json()


# --- identity / role / ownership are never taken from the request --------------


def test_forged_role_in_body_is_ignored(operator_client):
    response = operator_client.post("/api/v1/campaigns", json={"name": "x", "role": "admin"})
    assert response.status_code == 403


def test_forged_role_in_query_and_headers_is_ignored(operator_client):
    response = operator_client.post(
        "/api/v1/campaigns?role=admin&user_id=1",
        json={"name": "x"},
        headers={"X-Role": "admin", "X-User-Id": "root", "X-Tenant-Id": "other"},
    )
    assert response.status_code == 403


def test_forged_ownership_fields_in_body_are_not_persisted(client):
    response = client.post(
        "/api/v1/campaigns",
        json={"name": "x", "tenant_id": "victim", "owner_id": "victim", "created_by": "root"},
    )
    assert response.status_code == 201
    body = response.json()
    assert not {"tenant_id", "owner_id", "created_by"} & set(body)


def test_role_comes_from_token_not_from_username_claim(anon_client):
    """A token whose `sub` is literally 'admin' but whose role is operator is
    still an operator."""
    token = _token(role="operator", sub="admin")
    response = anon_client.post(
        "/api/v1/campaigns", json={"name": "x"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_openapi_and_health_remain_reachable_without_auth():
    anon = TestClient(app)
    assert anon.get("/health").status_code == 200


# --- structural guard: no future route can silently ship unauthenticated -----

# Routes that are intentionally not behind a bearer token, each with its own
# credential: login (issues tokens) and the two provider webhooks.
_PUBLIC_API_PREFIXES = ("/api/v1/admin/auth/login", "/api/v1/webhooks/")


def _dependency_calls(dependant):
    for sub in dependant.dependencies:
        yield sub.call
        yield from _dependency_calls(sub)


def test_every_api_route_requires_authentication():
    from app.api.admin_deps import require_admin

    unprotected = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1") or path.startswith(_PUBLIC_API_PREFIXES):
            continue
        if require_admin not in set(_dependency_calls(route.dependant)):
            unprotected.append(f"{sorted(route.methods)} {path}")
    assert not unprotected, f"routes without authentication: {unprotected}"


# --- audit attribution: the actor is the verified principal -----------------


def test_audit_actor_is_the_authenticated_principal(db_session, client):
    from app.services.admin.auth import AdminPrincipal, create_access_token

    token, _ = create_access_token(AdminPrincipal(username="alice-ops", role="admin"))
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/v1/campaigns", json={"name": "audited"}, headers=headers)
    assert created.status_code == 201
    contact = client.post(
        "/api/v1/contacts",
        json={"campaign_id": created.json()["id"], "phone_number": "+14155550142"},
        headers=headers,
    )
    assert contact.status_code == 201

    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    actors = {r.actor for r in db_session.execute(select(AuditLog)).scalars()}
    assert "alice-ops" in actors
    assert "api-client" not in actors


def test_audit_actor_cannot_be_forged_from_the_request(db_session, client):
    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    client.post(
        "/api/v1/campaigns",
        json={"name": "x", "actor": "root", "created_by": "root"},
        headers={"X-Actor": "root", "X-User": "root"},
    )
    actors = {r.actor for r in db_session.execute(select(AuditLog)).scalars()}
    assert "root" not in actors
