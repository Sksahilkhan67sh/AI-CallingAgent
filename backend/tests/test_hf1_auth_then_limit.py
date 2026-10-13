"""HF1 1.1 -- authenticate FIRST, then limit.

Reproductions of the audited defect: a flood of unauthenticated requests from one
client address used up the per-address bucket, so the next request carrying the CORRECT
secret was answered 429 (webhooks), and ten failed logins locked the real admin out
(login counted every request, successes included, per address only).

Written before the fix; every test marked "repro" failed on the unmodified code.
"""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.config import get_settings
from app.main import app

_LOGIN = "/api/v1/admin/auth/login"
_DOGRAH = "/api/v1/webhooks/dograh/call-completed"
_NATIVE = "/api/v1/webhooks/telephony/call-status"

# Obviously fake documentation-range addresses (RFC 5737).
_ADDR_A = "198.51.100.10"
_ADDR_B = "198.51.100.11"


class _PeerAddress:
    """ASGI wrapper that sets the socket peer (this Starlette's TestClient cannot)."""

    def __init__(self, inner, addr: str) -> None:
        self._inner, self._addr = inner, addr

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self._addr, 40000)}
        await self._inner(scope, receive, send)


def _from(addr: str) -> TestClient:
    return TestClient(_PeerAddress(app, addr))


def _dograh_body() -> dict:
    return {"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"}


def _dograh_ok() -> dict:
    return {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}


def _native_body() -> dict:
    return {"event_id": str(uuid.uuid4()), "provider_call_id": "x", "status": "connected"}


def _native_ok() -> dict:
    return {"X-Webhook-Secret": get_settings().telephony_webhook_secret}


@pytest.fixture(autouse=True)
def _fresh(client):  # `client` flushes Redis and installs the DB override
    rate_limit._last_degraded_audit.clear()
    yield
    rate_limit._last_degraded_audit.clear()


# --- webhooks ------------------------------------------------------------------------


def test_repro_dograh_valid_secret_is_not_throttled_by_failed_auth_flood():
    attacker = _from(_ADDR_A)
    for _ in range(121):
        assert attacker.post(_DOGRAH, json=_dograh_body()).status_code in (401, 429)
    response = attacker.post(_DOGRAH, json=_dograh_body(), headers=_dograh_ok())
    assert response.status_code not in (401, 429)  # reached processing (unknown attempt)


def test_repro_native_valid_secret_is_not_throttled_by_failed_auth_flood():
    attacker = _from(_ADDR_A)
    for _ in range(121):
        attacker.post(_NATIVE, json=_native_body(), headers={"X-Webhook-Secret": "wrong"})
    response = attacker.post(_NATIVE, json=_native_body(), headers=_native_ok())
    assert response.status_code not in (401, 429)


def test_repro_one_thousand_bad_requests_then_a_valid_one_succeeds():
    attacker = _from(_ADDR_A)
    codes = {attacker.post(_DOGRAH, json={}).status_code for _ in range(1000)}
    assert codes <= {401, 429}
    assert attacker.post(_DOGRAH, json=_dograh_body(), headers=_dograh_ok()).status_code not in (
        401,
        429,
    )


def test_failed_auth_is_401_until_the_counter_is_over_its_limit_then_429(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_rate_limit_per_minute", 3)
    attacker = _from(_ADDR_A)
    codes = [attacker.post(_DOGRAH, json={}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_failure_counters_do_not_leak_across_addresses(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_rate_limit_per_minute", 2)
    for _ in range(6):
        _from(_ADDR_A).post(_DOGRAH, json={})
    assert _from(_ADDR_A).post(_DOGRAH, json={}).status_code == 429
    assert _from(_ADDR_B).post(_DOGRAH, json={}).status_code == 401  # B has its own budget


def test_valid_secret_has_a_generous_ceiling_that_is_still_enforced(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_valid_ceiling_per_minute", 3)
    codes = [
        _from(_ADDR_A).post(_DOGRAH, json=_dograh_body(), headers=_dograh_ok()).status_code
        for _ in range(5)
    ]
    assert 429 not in codes[:3] and codes[3:] == [429, 429]


def test_default_valid_ceiling_is_well_above_admission_cps_times_60():
    settings = get_settings()
    assert settings.webhook_valid_ceiling_per_minute > settings.global_cps_limit * 60


# --- login ---------------------------------------------------------------------------


def _admin_creds() -> dict:
    settings = get_settings()
    return {"username": settings.admin_username, "password": settings.admin_password}


def test_repro_failed_logins_for_another_user_do_not_lock_out_the_real_admin():
    attacker = _from(_ADDR_A)
    for _ in range(10):
        bad = {"username": "nobody", "password": "x"}
        assert attacker.post(_LOGIN, json=bad).status_code == 401
    assert attacker.post(_LOGIN, json=_admin_creds()).status_code == 200


def test_repro_successful_logins_are_not_counted_as_failures(monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 3)
    codes = [_from(_ADDR_A).post(_LOGIN, json=_admin_creds()).status_code for _ in range(8)]
    assert codes == [200] * 8


def test_failures_for_the_same_user_and_address_are_limited(monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 2)
    bad = {"username": "someone", "password": "x"}
    codes = [_from(_ADDR_A).post(_LOGIN, json=bad).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]


def test_per_address_ceiling_blocks_username_spraying(monkeypatch):
    monkeypatch.setattr(get_settings(), "login_ip_failure_ceiling_per_minute", 4)
    attacker = _from(_ADDR_A)
    codes = [
        attacker.post(_LOGIN, json={"username": f"user{i}", "password": "x"}).status_code
        for i in range(6)
    ]
    assert codes == [401, 401, 401, 401, 429, 429]
    # ...and a correct credential from that same over-ceiling address is not exempt
    assert attacker.post(_LOGIN, json=_admin_creds()).status_code == 429


def test_login_limits_do_not_leak_across_addresses(monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 1)
    bad = {"username": "someone", "password": "x"}
    _from(_ADDR_A).post(_LOGIN, json=bad)
    assert _from(_ADDR_A).post(_LOGIN, json=bad).status_code == 429
    assert _from(_ADDR_B).post(_LOGIN, json=bad).status_code == 401
    assert _from(_ADDR_B).post(_LOGIN, json=_admin_creds()).status_code == 200


def test_a_huge_username_cannot_blow_up_the_key_space(redis_client):
    huge = {"username": "u" * 5000, "password": "x"}
    assert _from(_ADDR_A).post(_LOGIN, json=huge).status_code in (401, 422)
    keys = list(redis_client.scan_iter("ratelimit:*"))
    assert all(len(k) < 200 for k in keys)


# --- the limiter itself ---------------------------------------------------------------


def test_limiter_is_atomic_under_concurrent_threads_on_real_redis(redis_client):
    """Exactly `limit` callers pass, however the threads interleave, and the key never
    exists without a TTL (a poller watches for ttl == -1 the whole time)."""
    limit, threads, per_thread = 50, 8, 40
    passed, blocked, saw_no_ttl = [], [], []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            key = "ratelimit:atomic"
            if redis_client.exists(key) and redis_client.ttl(key) == -1:
                saw_no_ttl.append(True)

    def hammer() -> None:
        for _ in range(per_thread):
            try:
                rate_limit.check_rate_limit(
                    redis_client, key="atomic", limit=limit, window_seconds=60
                )
                passed.append(1)
            except rate_limit.RateLimitExceeded:
                blocked.append(1)

    poller = threading.Thread(target=poll)
    poller.start()
    workers = [threading.Thread(target=hammer) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    poller.join()
    assert len(passed) == limit
    assert len(blocked) == threads * per_thread - limit
    assert not saw_no_ttl


def test_failure_counter_never_exists_without_a_ttl(redis_client):
    rate_limit.record_failure(redis_client, key="authfail:x", window_seconds=30)
    assert 0 < redis_client.ttl("ratelimit:authfail:x") <= 30
