"""CP11 -- rate limiting is ENFORCED (not merely configured), its Redis-failure
policy is per-endpoint, and the obvious bypasses do not work."""

import uuid

import pytest
import redis as redis_lib
from sqlalchemy import select

from app.core import rate_limit
from app.core.config import get_settings
from app.models.audit_log import AuditLog

_LOGIN = "/api/v1/admin/auth/login"
_DOGRAH = "/api/v1/webhooks/dograh/call-completed"
_LEGACY = "/api/v1/webhooks/telephony/call-status"


def _bad_login():
    return {"username": "nobody", "password": "wrong"}


def _dograh_body():
    return {"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"}


def _dograh_auth():
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


@pytest.fixture(autouse=True)
def _reset_degraded_audit_throttle():
    rate_limit._last_degraded_audit.clear()
    yield
    rate_limit._last_degraded_audit.clear()


@pytest.fixture
def broken_limiter_redis(monkeypatch):
    bad = redis_lib.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_timeout=0.2, socket_connect_timeout=0.2
    )
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: bad)


# --- enforcement ---------------------------------------------------------------


def test_legacy_webhook_is_rate_limited_and_sends_retry_after(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_rate_limit_per_minute", 3)
    statuses = [
        anon_client.post(_LEGACY, json={}, headers={"X-Webhook-Secret": "nope"})
        for _ in range(5)
    ]
    assert [r.status_code for r in statuses] == [401, 401, 401, 429, 429]
    assert 1 <= int(statuses[-1].headers["retry-after"]) <= 60


def test_login_429_carries_retry_after(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 2)
    responses = [anon_client.post(_LOGIN, json=_bad_login()) for _ in range(3)]
    assert [r.status_code for r in responses] == [401, 401, 429]
    assert "retry-after" in responses[-1].headers


def test_limits_are_per_endpoint_not_shared(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 1)
    anon_client.post(_LOGIN, json=_bad_login())
    assert anon_client.post(_LOGIN, json=_bad_login()).status_code == 429
    # exhausting login must not throttle the webhook bucket
    assert anon_client.post(_LEGACY, json={}, headers={"X-Webhook-Secret": "x"}).status_code == 401


def test_a_rejected_caller_is_rejected_even_with_a_valid_secret(anon_client, monkeypatch):
    """The limit applies before authentication, so a flood cannot be 'paid for' with
    a valid credential either."""
    monkeypatch.setattr(get_settings(), "webhook_rate_limit_per_minute", 2)
    for _ in range(2):
        anon_client.post(_DOGRAH, json=_dograh_body(), headers=_dograh_auth())
    assert anon_client.post(_DOGRAH, json=_dograh_body(), headers=_dograh_auth()).status_code == 429


# --- bypass attempts ------------------------------------------------------------------


def test_rotating_x_forwarded_for_does_not_reset_the_bucket(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 2)
    codes = [
        anon_client.post(_LOGIN, json=_bad_login(), headers={"X-Forwarded-For": f"10.0.0.{i}"})
        .status_code
        for i in range(4)
    ]
    assert codes == [401, 401, 429, 429]


def test_trailing_slash_variant_is_still_counted(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 2)
    codes = [
        anon_client.post(path, json=_bad_login()).status_code
        for path in (_LOGIN, _LOGIN + "/", _LOGIN, _LOGIN + "/")
    ]
    assert codes[-1] == 429 and codes.count(401) == 2


def test_other_http_methods_do_not_reach_the_handler(anon_client):
    for method in ("get", "put", "delete", "patch"):
        assert getattr(anon_client, method)(_LOGIN).status_code in (404, 405)


# --- Redis failure policy (user-decided) --------------------------------------------


def test_login_fails_closed_with_503_when_limiter_is_down(anon_client, broken_limiter_redis):
    settings = get_settings()
    response = anon_client.post(
        _LOGIN, json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert response.status_code == 503  # even CORRECT credentials get no token
    assert "access_token" not in response.text


def test_dograh_webhook_fails_open_but_authentication_never_does(
    anon_client, broken_limiter_redis
):
    no_creds = anon_client.post(_DOGRAH, json=_dograh_body())
    wrong = anon_client.post(_DOGRAH, json=_dograh_body(), headers={"X-API-Key": "wrong"})
    assert no_creds.status_code == 401 and wrong.status_code == 401

    ok = anon_client.post(_DOGRAH, json=_dograh_body(), headers=_dograh_auth())
    # reached processing (unknown attempt -> 404); neither throttled nor blocked
    assert ok.status_code not in (401, 429, 503)


def test_legacy_webhook_fails_open_but_authentication_never_does(
    anon_client, broken_limiter_redis
):
    assert anon_client.post(_LEGACY, json={}, headers={"X-Webhook-Secret": "x"}).status_code == 401
    ok = anon_client.post(
        _LEGACY,
        json={"event_id": "e", "provider_call_id": "x", "status": "connected"},
        headers={"X-Webhook-Secret": get_settings().telephony_webhook_secret},
    )
    assert ok.status_code not in (401, 422, 429, 503)


def test_fail_open_is_audited_once_per_throttle_window(
    anon_client, broken_limiter_redis, db_session
):
    for _ in range(5):
        anon_client.post(_DOGRAH, json=_dograh_body(), headers=_dograh_auth())
    rows = (
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "rate_limit.unavailable_failed_open")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_metadata == {"endpoint": "dograh_webhook"}
    assert rows[0].actor == "rate-limiter"


def test_webhook_idempotency_does_not_depend_on_the_limiter_redis(
    anon_client, broken_limiter_redis
):
    """Same payload twice with the limiter down: both are processed by the DB-backed
    path (same outcome both times), nothing is lost or double-processed by the limiter."""
    body = _dograh_body()
    first = anon_client.post(_DOGRAH, json=body, headers=_dograh_auth())
    second = anon_client.post(_DOGRAH, json=body, headers=_dograh_auth())
    assert first.status_code == second.status_code


# --- the limiter itself ---------------------------------------------------------------


def test_key_without_ttl_is_healed_not_blocked_forever(redis_client):
    """INCR succeeded, process died before EXPIRE: the key has no TTL. Once over the
    limit the next call must give it one, so the block ends."""
    key = "ratelimit:heal-test"
    redis_client.set(key, 999)  # no TTL
    assert redis_client.ttl(key) == -1
    with pytest.raises(rate_limit.RateLimitExceeded) as exc:
        rate_limit.check_rate_limit(redis_client, key="heal-test", limit=5, window_seconds=30)
    assert redis_client.ttl(key) > 0
    assert exc.value.retry_after == 30


def test_check_rate_limit_reports_degraded_on_fail_open_and_raises_on_fail_closed():
    class _Broken:
        def incr(self, key):
            raise redis_lib.RedisError("down")

    assert rate_limit.check_rate_limit(_Broken(), key="k", limit=1, window_seconds=60) is True
    with pytest.raises(rate_limit.RateLimiterUnavailable):
        rate_limit.check_rate_limit(
            _Broken(), key="k", limit=1, window_seconds=60, fail_closed=True
        )


def test_keys_expire_so_memory_is_bounded(redis_client):
    rate_limit.check_rate_limit(redis_client, key="bounded", limit=10, window_seconds=45)
    assert 0 < redis_client.ttl("ratelimit:bounded") <= 45
