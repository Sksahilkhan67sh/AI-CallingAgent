"""Checkpoint 09 §8.5 -- rate limiting on public endpoints."""

from app.core.config import get_settings


def test_login_is_rate_limited_after_threshold(client):
    limit = get_settings().login_rate_limit_per_minute
    for _ in range(limit):
        client.post(
            "/api/v1/admin/auth/login", json={"username": "nobody", "password": "wrong"}
        )

    response = client.post(
        "/api/v1/admin/auth/login", json={"username": "nobody", "password": "wrong"}
    )
    assert response.status_code == 429


def test_webhook_is_rate_limited_after_threshold(client):
    limit = get_settings().webhook_rate_limit_per_minute
    headers = {"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"}
    import uuid

    for _ in range(limit):
        client.post(
            "/api/v1/webhooks/dograh/call-completed",
            json={"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"},
            headers=headers,
        )

    response = client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"},
        headers=headers,
    )
    assert response.status_code == 429


def test_rate_limiter_fails_open_on_redis_error(monkeypatch):
    """§12: an unreachable rate limiter must never itself take the
    endpoint down."""
    from app.core.rate_limit import check_rate_limit

    class _BrokenRedis:
        def incr(self, key):
            import redis

            raise redis.RedisError("down")

    # Should not raise -- fails open.
    check_rate_limit(_BrokenRedis(), key="test", limit=1, window_seconds=60)
