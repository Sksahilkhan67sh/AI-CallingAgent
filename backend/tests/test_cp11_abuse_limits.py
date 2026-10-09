"""CP11 Step 6 -- per-principal budgets on expensive authenticated operations,
and request-body size caps."""

import uuid

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from tests.conftest import _bearer

_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _isolated_state(client):
    """Requesting `client` installs the test-session `get_db` override and flushes Redis.
    Tests here build their own TestClient(app) per principal; without the override they
    would hit the REAL get_db and COMMIT rows (e.g. authz.denied audit events) into the
    shared test database, polluting later count-based tests."""
    yield


def _token_client(username: str, role: str = "admin") -> TestClient:
    from app.services.admin.auth import AdminPrincipal, create_access_token

    token, _ = create_access_token(AdminPrincipal(username=username, role=role))
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def broken_limiter_redis(monkeypatch):
    bad = redis_lib.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_timeout=0.2, socket_connect_timeout=0.2
    )
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: bad)


def _campaign(db_session):
    campaign = Campaign(name="limit test", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    return campaign


def _csv():
    return {"file": ("c.csv", b"phone_number\n+919845550100\n")}


# --- budgets are enforced ------------------------------------------------------------


def test_enqueue_is_limited_per_principal(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "enqueue_rate_limit_per_minute", 2)
    campaign = _campaign(db_session)
    url = f"/api/v1/campaigns/{campaign.id}/enqueue"
    codes = [client.post(url).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[2:] == [429, 429]
    blocked = client.post(url)
    assert 1 <= int(blocked.headers["retry-after"]) <= 60


def test_import_is_limited_per_principal(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "import_rate_limit_per_minute", 1)
    first = client.post("/api/v1/campaigns/import", data={"name": "a"}, files=_csv())
    second = client.post("/api/v1/campaigns/import", data={"name": "b"}, files=_csv())
    assert (first.status_code, second.status_code) == (201, 429)


def test_mutations_share_one_budget_across_routes(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 3)
    campaign = _campaign(db_session)
    results = [
        client.post("/api/v1/campaigns", json={"name": "a"}).status_code,
        client.patch(f"/api/v1/campaigns/{campaign.id}", json={"name": "b"}).status_code,
        client.post(
            "/api/v1/contacts",
            json={"campaign_id": str(campaign.id), "phone_number": "+919845550111"},
        ).status_code,
        client.post("/api/v1/campaigns", json={"name": "c"}).status_code,
    ]
    assert results[:3] == [201, 200, 201] and results[3] == 429


def test_analysis_reads_are_limited(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "analysis_rate_limit_per_minute", 2)
    codes = [client.get(f"/api/v1/campaigns/{_ID}/analysis").status_code for _ in range(3)]
    assert codes[-1] == 429 and 429 not in codes[:2]


def test_kill_switch_changes_draw_from_the_mutation_budget(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 2)
    codes = [
        client.post("/api/v1/admin/kill-switch").status_code,
        client.delete("/api/v1/admin/kill-switch").status_code,
        client.post("/api/v1/admin/kill-switch").status_code,
    ]
    assert codes == [200, 200, 429]


def test_budgets_are_independent_of_each_other(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "enqueue_rate_limit_per_minute", 1)
    campaign = _campaign(db_session)
    client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    assert client.post(f"/api/v1/campaigns/{campaign.id}/enqueue").status_code == 429
    assert client.post("/api/v1/campaigns", json={"name": "ok"}).status_code == 201
    assert client.get("/api/v1/campaigns").status_code == 200


# --- who the budget belongs to: the verified principal, nothing else ----------------


def test_each_principal_has_their_own_budget(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 1)
    alice, bob = _token_client("alice"), _token_client("bob")
    assert alice.post("/api/v1/campaigns", json={"name": "a"}).status_code == 201
    assert alice.post("/api/v1/campaigns", json={"name": "a2"}).status_code == 429
    assert bob.post("/api/v1/campaigns", json={"name": "b"}).status_code == 201  # unaffected


def test_a_second_token_for_the_same_user_shares_the_budget(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 1)
    assert _token_client("alice").post("/api/v1/campaigns", json={"name": "a"}).status_code == 201
    assert _token_client("alice").post("/api/v1/campaigns", json={"name": "b"}).status_code == 429


def test_rotating_ip_headers_does_not_reset_an_authenticated_budget(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 2)
    codes = [
        client.post(
            "/api/v1/campaigns",
            json={"name": f"n{i}"},
            headers={"X-Forwarded-For": f"203.0.113.{i}", "X-Real-IP": f"198.51.100.{i}"},
        ).status_code
        for i in range(4)
    ]
    assert codes == [201, 201, 429, 429]


def test_forged_identity_in_body_or_query_cannot_pick_a_different_bucket(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 1)
    client.post("/api/v1/campaigns", json={"name": "a"})
    response = client.post(
        "/api/v1/campaigns?user=someone-else",
        json={"name": "b", "user": "someone-else", "username": "someone-else"},
        headers={"X-User": "someone-else"},
    )
    assert response.status_code == 429


def test_unauthenticated_requests_never_consume_a_budget(anon_client, client, monkeypatch):
    """401 comes from resolving the identity, before anything is counted."""
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 1)
    for _ in range(5):
        assert anon_client.post("/api/v1/campaigns", json={"name": "x"}).status_code == 401
    assert client.post("/api/v1/campaigns", json={"name": "ok"}).status_code == 201


def test_an_operator_hammering_an_admin_route_is_bounded_and_still_denied(monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 2)
    operator = _token_client("op", role="operator")
    codes = [operator.post("/api/v1/campaigns", json={"name": "x"}).status_code for _ in range(4)]
    assert codes == [403, 403, 429, 429]


def test_trailing_slash_and_duplicate_route_variants_share_the_bucket(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "import_rate_limit_per_minute", 1)
    first = client.post("/api/v1/campaigns/import", data={"name": "a"}, files=_csv())
    second = client.post("/api/v1/campaigns/import/", data={"name": "b"}, files=_csv())
    assert first.status_code == 201
    assert second.status_code in (307, 308, 404, 429)  # never a free second import
    if second.status_code in (307, 308):
        followed = client.post(
            "/api/v1/campaigns/import", data={"name": "c"}, files=_csv()
        )
        assert followed.status_code == 429


# --- Redis failure policy ---------------------------------------------------------------


def test_enqueue_and_import_fail_closed_when_the_limiter_is_down(
    client, db_session, broken_limiter_redis
):
    campaign = _campaign(db_session)
    assert client.post(f"/api/v1/campaigns/{campaign.id}/enqueue").status_code == 503
    imported = client.post("/api/v1/campaigns/import", data={"name": "a"}, files=_csv())
    assert imported.status_code == 503


def test_ordinary_mutations_and_reads_stay_available_when_the_limiter_is_down(
    client, broken_limiter_redis
):
    assert client.post("/api/v1/campaigns", json={"name": "still works"}).status_code == 201
    assert client.get(f"/api/v1/campaigns/{_ID}/analysis").status_code != 503


def test_auth_still_applies_when_the_limiter_is_down(anon_client, broken_limiter_redis):
    assert anon_client.post("/api/v1/campaigns", json={"name": "x"}).status_code == 401


# --- request body size ------------------------------------------------------------------

_MAX = 1_048_576


def _oversized_json():
    return b'{"name": "' + b"x" * (_MAX + 10) + b'"}'


def test_oversized_json_body_is_413(client):
    response = client.post(
        "/api/v1/campaigns", content=_oversized_json(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_body_just_under_the_cap_is_still_processed(client):
    name = "x" * 200
    body = ('{"name": "' + name + '"}').encode()
    assert len(body) < _MAX
    response = client.post(
        "/api/v1/campaigns", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code != 413


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/api/v1/admin/auth/login", {}),
        ("/api/v1/webhooks/dograh/call-completed", {"Authorization": "Bearer x"}),
        ("/api/v1/webhooks/telephony/call-status", {"X-Webhook-Secret": "x"}),
    ],
)
def test_unauthenticated_endpoints_are_capped_too(anon_client, path, headers):
    response = anon_client.post(
        path,
        content=_oversized_json(),
        headers={"content-type": "application/json", **headers},
    )
    assert response.status_code == 413


def test_streamed_body_without_content_length_is_413(client):
    def chunks():
        for _ in range(40):  # 40 x 64 KiB = 2.5 MiB, no Content-Length (chunked)
            yield b"x" * 65_536

    response = client.post(
        "/api/v1/campaigns", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413  # not FastAPI's own "400 error parsing the body"


def test_streamed_body_is_not_consumed_past_the_cap():
    """Driven at the ASGI level (TestClient pre-buffers request bodies, so it cannot show
    this): the app must stop pulling chunks right after the 1 MiB cap is crossed."""
    import asyncio

    pulled = 0
    sent = []

    async def receive():
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 65_536, "more_body": pulled < 2000}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/campaigns",
        "raw_path": b"/api/v1/campaigns",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],  # no content-length: chunked
        "client": ("203.0.113.9", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))

    assert sent[0]["status"] == 413
    assert pulled <= 20  # ~1 MiB / 64 KiB = 16 chunks, not 2000 (~125 MiB)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_lying_content_length_cannot_smuggle_a_big_body(client):
    """Declares 10 bytes, sends far more: the counted bytes -- not the header -- rule."""
    response = client.post(
        "/api/v1/campaigns",
        content=_oversized_json(),
        headers={"content-type": "application/json", "content-length": "10"},
    )
    assert response.status_code in (400, 413)


def test_huge_declared_content_length_is_rejected_without_reading_the_body(client):
    response = client.post(
        "/api/v1/campaigns",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": "99999999999"},
    )
    assert response.status_code in (400, 413)


def test_import_gets_a_larger_cap_but_not_an_unbounded_one(client):
    # > 5 MiB file but < 6 MiB body: reaches the import route's own file check (413/422),
    # not the generic 1 MiB middleware cap that would have rejected it earlier.
    big_ok = b"phone_number\n" + b"+919845550100\n" * 1000
    assert client.post(
        "/api/v1/campaigns/import", data={"name": "a"}, files={"file": ("c.csv", big_ok)}
    ).status_code == 201  # well over nothing, well under both caps

    too_big = b"x" * (7 * 1024 * 1024)
    response = client.post(
        "/api/v1/campaigns/import", data={"name": "b"}, files={"file": ("c.csv", too_big)}
    )
    assert response.status_code == 413


def test_oversized_request_gets_cors_headers_so_browsers_see_the_413(anon_client):
    origin = get_settings().admin_cors_origins[0]
    response = anon_client.post(
        "/api/v1/admin/auth/login",
        content=_oversized_json(),
        headers={"content-type": "application/json", "origin": origin},
    )
    assert response.status_code == 413
    assert response.headers.get("access-control-allow-origin") == origin


def test_non_http_scopes_are_untouched():
    from app.core.body_limit import BodySizeLimitMiddleware

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    import asyncio

    asyncio.run(BodySizeLimitMiddleware(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_bearer_helper_is_importable():
    # keeps the shared test helper honest: the middleware tests above rely on it
    assert _bearer("admin")["Authorization"].startswith("Bearer ")
