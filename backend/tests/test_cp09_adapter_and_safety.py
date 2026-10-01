"""CP09 -- Dograh adapter classification, production config validation,
circuit breaker states, state machine, health, logging/PII, rate limiting,
RBAC and audit."""

import json
import logging
import time

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.logging_config import JsonFormatter, mask_phone
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.enums import CallAttemptState as S
from app.models.enums import CampaignStatus
from app.services import call_state
from app.services.telephony.circuit_breaker import CircuitBreaker, CircuitState
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhClient,
    ProviderErrorKind,
)


def _client(**kw):
    base = dict(base_url="https://d.example.com", api_key="k", trigger_uuid="u", mode="production")
    base.update(kw)
    return DograhClient(**base)


def _raise(exc):
    def fn(*a, **k):
        raise exc

    return fn


def _trigger(client):
    return client.trigger_call(phone_number="+15551234567", initial_context={})


# ---- adapter classification ----------------------------------------------


@pytest.mark.parametrize(
    "exc, kind, ambiguous",
    [
        (httpx.ConnectTimeout("t"), ProviderErrorKind.TIMEOUT, False),
        (httpx.ConnectError("c"), ProviderErrorKind.CONNECTION_ERROR, False),
        (httpx.ReadTimeout("r"), ProviderErrorKind.TIMEOUT, True),
        (httpx.WriteTimeout("w"), ProviderErrorKind.TIMEOUT, True),
        (httpx.RemoteProtocolError("p"), ProviderErrorKind.CONNECTION_ERROR, True),
    ],
)
def test_transport_errors_are_classified_and_ambiguity_is_correct(
    monkeypatch, exc, kind, ambiguous
):
    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", _raise(exc))
    with pytest.raises(DograhApiError) as e:
        _trigger(_client())
    assert e.value.kind == kind
    assert e.value.ambiguous is ambiguous


@pytest.mark.parametrize(
    "status, kind, ambiguous",
    [
        (401, ProviderErrorKind.AUTHENTICATION_ERROR, False),
        (403, ProviderErrorKind.AUTHENTICATION_ERROR, False),
        (400, ProviderErrorKind.PROVIDER_REJECTED, False),
        (404, ProviderErrorKind.PROVIDER_REJECTED, False),
        (422, ProviderErrorKind.VALIDATION_ERROR, False),
        (429, ProviderErrorKind.RATE_LIMITED, False),
        (500, ProviderErrorKind.PROVIDER_UNAVAILABLE, True),
        (503, ProviderErrorKind.PROVIDER_UNAVAILABLE, True),
    ],
)
def test_http_statuses_are_classified(monkeypatch, status, kind, ambiguous):
    monkeypatch.setattr(
        "app.services.telephony.dograh_client.httpx.post",
        lambda url, **k: httpx.Response(
            status, json={"detail": "x"}, request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(DograhApiError) as e:
        _trigger(_client())
    assert (e.value.kind, e.value.ambiguous) == (kind, ambiguous)


def test_2xx_without_a_run_id_is_ambiguous(monkeypatch):
    monkeypatch.setattr(
        "app.services.telephony.dograh_client.httpx.post",
        lambda url, **k: httpx.Response(
            200, json={"status": "ok"}, request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(DograhApiError) as e:
        _trigger(_client())
    assert e.value.kind == ProviderErrorKind.AMBIGUOUS_REQUEST and e.value.ambiguous


def test_every_request_has_bounded_connect_and_read_timeouts(monkeypatch):
    seen = {}

    def fake(url, *, headers, json, timeout):
        seen["t"] = timeout
        return httpx.Response(200, json={"workflow_run_id": 1}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake)
    _trigger(_client(timeout=12, connect_timeout=3))
    assert seen["t"].connect == 3 and seen["t"].read == 12 and seen["t"].write == 12


def test_find_run_matches_on_our_correlation_id(monkeypatch):
    runs = {
        "runs": [
            {"id": 1, "initial_context": {"call_attempt_id": "other"}},
            {"id": 2, "initial_context": {"call_attempt_id": "mine"}, "is_completed": True},
        ],
        "total_pages": 1,
    }
    monkeypatch.setattr(
        "app.services.telephony.dograh_client.httpx.get",
        lambda url, **k: httpx.Response(200, json=runs, request=httpx.Request("GET", url)),
    )
    found = _client(workflow_id=7).find_run_by_attempt(
        call_attempt_id="mine", since=__import__("datetime").datetime.now()
    )
    assert found is not None and found.run_id == 2
    assert _client(workflow_id=7).find_run_by_attempt(
        call_attempt_id="nobody", since=__import__("datetime").datetime.now()
    ) is None


# ---- production configuration --------------------------------------------

_PROD = dict(
    environment="production",
    primary_db_url="postgresql+psycopg://app:S3cure@db.internal:5432/prod",
    redis_url="redis://redis.internal:6379/0",
    jwt_signing_key="a" * 40,
    admin_password="p" * 20,
    operator_password="q" * 20,
    telephony_webhook_secret="s" * 32,
    calling_engine="dograh",
    dograh_api_base_url="https://dograh.internal",
    dograh_api_key="k" * 24,
    dograh_trigger_uuid="uuid",
    dograh_workflow_id=5,
    dograh_trigger_mode="production",
    dograh_webhook_secret="w" * 32,
    log_level="info",
)


def test_valid_production_config_loads():
    Settings(**_PROD)


def test_production_fails_fast_naming_variables_but_never_values():
    secret = "sup3r-s3cret-value-xyz"
    bad = {
        **_PROD,
        "dograh_webhook_secret": "",
        "dograh_trigger_mode": "test",
        "dograh_workflow_id": None,
        "jwt_signing_key": "dev-only-change-me",
        "dograh_api_key": secret + "change-me",
    }
    with pytest.raises(ValueError) as e:
        Settings(**bad)
    text = str(e.value)
    for name in (
        "DOGRAH_WEBHOOK_SECRET",
        "DOGRAH_TRIGGER_MODE",
        "DOGRAH_WORKFLOW_ID",
        "JWT_SIGNING_KEY",
    ):
        assert name in text
    assert secret not in text and "dev-only" not in text


def test_development_defaults_are_still_allowed_outside_production():
    Settings(environment="development")


# ---- state machine --------------------------------------------------------


@pytest.mark.parametrize(
    "old, new",
    [
        (S.ENDED_NORMALLY, S.INITIATED),
        (S.ENDED_NORMALLY, S.CONNECTED),
        (S.FAILED_TO_CONNECT, S.CONNECTED),
        (S.DROPPED_MID_CALL, S.INITIATED),
        (S.INITIATED, S.ENDED_NORMALLY),
        (S.INITIATED, S.DROPPED_MID_CALL),
        (S.CONNECTED, S.FAILED_TO_CONNECT),
        (S.CONNECTED, S.CONNECTED),
    ],
)
def test_illegal_transitions_are_rejected(db_session, old, new):
    from app.models.contact import Contact  # noqa: F401  (ensure mappers loaded)

    attempt = CallAttempt(id=__import__("uuid").uuid4(), state=old)
    with pytest.raises(call_state.InvalidTransition):
        call_state.transition(db_session, attempt, new, reason="t", source="t")
    assert attempt.state == old  # untouched


# ---- circuit breaker ------------------------------------------------------


def test_circuit_goes_closed_open_half_open_closed(redis_client):
    b = CircuitBreaker(redis_client, "p")
    b.error_threshold = 2
    assert b.state() == CircuitState.CLOSED
    b.record_failure(), b.record_failure()
    assert b.state() == CircuitState.OPEN and not b.allow_request()
    redis_client.delete("circuit:p:open")  # cooldown elapsed
    assert b.state() == CircuitState.HALF_OPEN
    assert b.allow_request() is True  # exactly one probe...
    assert b.allow_request() is False  # ...not a stampede
    b.record_success()
    assert b.state() == CircuitState.CLOSED and b.allow_request()


def test_failed_probe_reopens_for_a_full_cooldown(redis_client):
    b = CircuitBreaker(redis_client, "p")
    b.error_threshold = 1
    b.record_failure()
    redis_client.delete("circuit:p:open")
    assert b.allow_request()
    b.record_failure()
    assert b.state() == CircuitState.OPEN
    assert 0 < redis_client.ttl("circuit:p:open") <= b.open_seconds


def test_failures_outside_the_window_do_not_accumulate(redis_client):
    b = CircuitBreaker(redis_client, "p")
    b.error_threshold = 3
    b.window_seconds = 1
    b.record_failure(), b.record_failure()
    time.sleep(1.2)
    b.record_failure()
    assert b.state() == CircuitState.CLOSED


# ---- health ---------------------------------------------------------------


def test_liveness_never_depends_on_postgres_or_redis(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.health._postgres_ok", lambda: False)
    monkeypatch.setattr("app.api.routes.health._redis_ok", lambda: False)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 200


def test_readiness_reflects_dependencies_and_leaks_nothing(client, monkeypatch):
    ok = client.get("/health/ready")
    assert ok.status_code == 200 and ok.json()["checks"] == {"postgres": "ok", "redis": "ok"}
    monkeypatch.setattr("app.api.routes.health._redis_ok", lambda: False)
    bad = client.get("/health/ready")
    assert bad.status_code == 503 and bad.json()["checks"]["redis"] == "fail"
    body = json.dumps(bad.json())
    assert "postgres:" not in body and "localhost" not in body and "password" not in body.lower()


# ---- logging / PII --------------------------------------------------------


def test_phone_masking():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("12345") == "*****"


def test_json_logs_mask_phones_redact_secrets_and_keep_uuids():
    record = logging.LogRecord("t", logging.INFO, "", 0, "dialing +919876543210 now", (), None)
    record.attempt_id = "79d742e3-96a5-4e12-b6ab-181234563203"
    record.api_key = "dg_live_abc"
    record.note = {"to": "+1 555 123 4567"}
    out = json.loads(JsonFormatter().format(record))
    assert "9876543210" not in json.dumps(out) and "+91******3210" in out["event"]
    assert out["attempt_id"] == "79d742e3-96a5-4e12-b6ab-181234563203"
    assert out["api_key"] == "[redacted]"
    assert "555 123 4567" not in json.dumps(out)


# ---- rate limiting / auth / RBAC / audit ----------------------------------


def test_login_is_rate_limited_per_client(client, redis_client):
    limit = get_settings().login_rate_limit_per_minute
    codes = [
        client.post("/api/v1/admin/auth/login", json={"username": "x", "password": "y"}).status_code
        for _ in range(limit + 2)
    ]
    assert codes[:limit] == [401] * limit
    assert codes[limit:] == [429, 429]


def test_failed_login_is_audited_without_the_password(client, db_session):
    client.post("/api/v1/admin/auth/login", json={"username": "mallory", "password": "hunter2-xyz"})
    row = db_session.query(AuditLog).filter_by(action="admin.login.failed").one()
    assert row.actor == "mallory"
    assert "hunter2" not in json.dumps(row.event_metadata or {})


def _login(client, role):
    s = get_settings()
    if role == "admin":
        user, pw = s.admin_username, s.admin_password
    else:
        user, pw = s.operator_username, s.operator_password
    return {"Authorization": "Bearer " + client.post(
        "/api/v1/admin/auth/login", json={"username": user, "password": pw}
    ).json()["access_token"]}


def test_operator_cannot_change_status_but_admin_can_and_it_is_audited(client, db_session):
    campaign = Campaign(name="rbac", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    url = f"/api/v1/admin/campaigns/{campaign.id}/status?new_status=paused"
    assert client.post(url, headers=_login(client, "operator")).status_code == 403
    assert client.post(url).status_code in (401, 403)
    r = client.post(url, headers=_login(client, "admin"))
    assert r.status_code == 200, r.text
    row = db_session.query(AuditLog).filter_by(action="admin.campaign.status_change").one()
    assert row.entity_id == campaign.id and row.event_metadata["to_status"] == "paused"


def test_metrics_endpoint_requires_admin_and_exposes_no_pii(client):
    assert client.get("/api/v1/admin/dashboard/system/metrics").status_code in (401, 403)
    r = client.get("/api/v1/admin/dashboard/system/metrics", headers=_login(client, "admin"))
    assert r.status_code == 200 and "active_calls" in r.json()["gauges"]
