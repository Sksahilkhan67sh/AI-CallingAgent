"""CP11 Step 8 -- security audit events, correlation IDs, and PII/secret-safe logging."""

import json
import logging
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core import request_context
from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.services import kill_switch, security_audit
from tests.test_dograh_webhook import _connected_call, _headers

_LOGIN = "/api/v1/admin/auth/login"
_DOGRAH = "/api/v1/webhooks/dograh/call-completed"
_LEGACY = "/api/v1/webhooks/telephony/call-status"


@pytest.fixture(autouse=True)
def _reset_throttle():
    security_audit._last_written.clear()
    yield
    security_audit._last_written.clear()


@pytest.fixture(autouse=True)
def _security_logger_enabled():
    """alembic/env.py runs logging.config.fileConfig() with disable_existing_loggers=True
    and tests/test_migrations.py runs it in-process, silently disabling every logger that
    already exists. caplog-based tests here would then see nothing -- re-enable ours."""
    logging.getLogger("app.security").disabled = False
    yield


def _rows(db_session, action, *responses):
    """Audit rows for `action`. When responses are given, only rows written while serving
    THOSE requests (matched by request_id) -- so the assertions hold regardless of what other
    tests have committed to the shared test database."""
    rows = db_session.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
    if responses:
        ids = {r.headers["x-request-id"] for r in responses}
        rows = [r for r in rows if (r.event_metadata or {}).get("request_id") in ids]
    return rows


def _blob(row) -> str:
    """Everything persisted for a row, as one searchable string."""
    return json.dumps(
        {"actor": row.actor, "action": row.action, "meta": row.event_metadata}, default=str
    )


# --- authorization denial (403) -----------------------------------------------------------


def test_role_denial_is_audited_with_actor_route_and_request_id(operator_client, db_session):
    response = operator_client.post("/api/v1/campaigns", json={"name": "x"})
    assert response.status_code == 403

    (row,) = _rows(db_session, "authz.denied", response)
    assert row.actor == "test-operator"
    assert row.event_metadata["role"] == "operator"
    assert row.event_metadata["required_role"] == "admin"
    assert row.event_metadata["method"] == "POST"
    assert row.event_metadata["route"] == "/api/v1/campaigns"
    assert row.event_metadata["request_id"] == response.headers["x-request-id"]


def test_denial_records_resource_ids_from_the_route_but_never_the_body(
    operator_client, db_session
):
    campaign_id = uuid.uuid4()
    response = operator_client.post(
        f"/api/v1/campaigns/{campaign_id}/enqueue?token=SECRETQUERY", json={"x": "SECRETBODY"}
    )
    (row,) = _rows(db_session, "authz.denied", response)
    assert row.event_metadata["resource_ids"] == {"campaign_id": str(campaign_id)}
    assert row.event_metadata["route"] == "/api/v1/campaigns/{campaign_id}/enqueue"
    assert "SECRETQUERY" not in _blob(row) and "SECRETBODY" not in _blob(row)


def test_repeated_denials_write_one_row_but_every_one_is_logged(
    operator_client, db_session, caplog
):
    responses = []
    with caplog.at_level(logging.WARNING, logger="app.security"):
        for _ in range(5):
            responses.append(operator_client.post("/api/v1/campaigns", json={"name": "x"}))
    assert all(r.status_code == 403 for r in responses)
    assert len(_rows(db_session, "authz.denied", *responses)) == 1  # DB sampled per actor+route
    assert sum(r.message == "security_event" for r in caplog.records) == 5  # log is complete


def test_authorized_requests_produce_no_denial_rows(client, db_session):
    response = client.post("/api/v1/campaigns", json={"name": "fine"})
    assert _rows(db_session, "authz.denied", response) == []


def test_anonymous_401_is_not_written_to_the_database(anon_client, db_session):
    """Unauthenticated floods must not become database writes (log-only)."""
    before = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    for _ in range(5):
        assert anon_client.post("/api/v1/campaigns", json={"name": "x"}).status_code == 401
    after = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert after == before


# --- login ----------------------------------------------------------------------------------


def test_failed_login_is_audited_without_username_or_password(anon_client, db_session):
    response = anon_client.post(
        _LOGIN, json={"username": "pasted-the-password-here", "password": "hunter2-wrong"}
    )
    assert response.status_code == 401
    (row,) = _rows(db_session, "auth.login_failed", response)
    assert row.actor == "anonymous"
    assert len(row.event_metadata["username_fingerprint"]) == 12
    assert row.event_metadata["source_ip"]
    blob = _blob(row)
    assert "pasted-the-password-here" not in blob and "hunter2" not in blob


def test_failed_login_rows_are_throttled_per_source(anon_client, db_session):
    responses = [
        anon_client.post(_LOGIN, json={"username": "x", "password": "y"}) for _ in range(4)
    ]
    assert len(_rows(db_session, "auth.login_failed", *responses)) == 1


def test_successful_login_is_audited_with_the_principal_and_no_token(anon_client, db_session):
    settings = get_settings()
    response = anon_client.post(
        _LOGIN, json={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert response.status_code == 200
    (row,) = _rows(db_session, "auth.login_succeeded", response)
    assert row.actor == settings.admin_username
    assert row.event_metadata["role"] == "admin"
    blob = _blob(row)
    assert response.json()["access_token"] not in blob
    assert settings.admin_password not in blob


# --- webhooks -------------------------------------------------------------------------------


def test_dograh_webhook_auth_failure_is_audited_without_the_credential(anon_client, db_session):
    attempted = "attacker-guessed-secret-123"
    response = anon_client.post(
        _DOGRAH,
        json={"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"},
        headers={"Authorization": f"Bearer {attempted}"},
    )
    assert response.status_code == 401
    (row,) = _rows(db_session, "webhook.auth_failed", response)
    assert row.entity_type == "webhook"
    assert row.event_metadata["endpoint"] == "dograh"
    assert row.event_metadata["credential_presented"] is True
    assert attempted not in _blob(row)


def test_missing_credential_is_distinguished_from_a_wrong_one(anon_client, db_session):
    response = anon_client.post(_DOGRAH, json={"call_attempt_id": str(uuid.uuid4())})
    (row,) = _rows(db_session, "webhook.auth_failed", response)
    assert row.event_metadata["credential_presented"] is False


def test_legacy_webhook_auth_failure_is_audited(anon_client, db_session):
    response = anon_client.post(
        _LEGACY, json={}, headers={"X-Webhook-Secret": "legacy-wrong-secret"}
    )
    (row,) = _rows(db_session, "webhook.auth_failed", response)
    assert row.event_metadata["endpoint"] == "telephony"
    assert "legacy-wrong-secret" not in _blob(row)


def test_webhook_auth_failure_rows_are_throttled_per_source(anon_client, db_session):
    responses = [
        anon_client.post(_DOGRAH, json={}, headers={"Authorization": "Bearer nope"})
        for _ in range(6)
    ]
    assert len(_rows(db_session, "webhook.auth_failed", *responses)) == 1


def test_duplicate_dograh_delivery_is_audited_and_still_idempotent(client, db_session):
    _, _, attempt = _connected_call(db_session, phone="989-960-0099")
    payload = {"call_attempt_id": str(attempt.id), "call_status": "user_hangup"}
    first = client.post(_DOGRAH, json=payload, headers=_headers())
    second = client.post(_DOGRAH, json=payload, headers=_headers())

    assert first.json()["outcome"] == "ended_normally"
    assert second.json()["outcome"] == "already_processed"
    (row,) = _rows(db_session, "webhook.duplicate", first, second)  # only the duplicate
    assert row.entity_id == attempt.id
    assert row.event_metadata["request_id"] == second.headers["x-request-id"]


# --- enqueue blocked by the kill switch --------------------------------------------------------


def test_enqueue_blocked_by_kill_switch_is_audited_and_survives_the_409(client, db_session):
    campaign = Campaign(name="blocked", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    kill_switch.enable("admin", None)

    response = client.post(f"/api/v1/campaigns/{campaign.id}/enqueue")
    assert response.status_code == 409

    (row,) = _rows(db_session, "campaign.enqueue_blocked", response)
    assert row.actor == "test-admin"
    assert row.entity_id == campaign.id
    assert row.event_metadata["reason"] == kill_switch.ENABLED


# --- correlation IDs ----------------------------------------------------------------------------


def test_every_response_carries_a_request_id(client, anon_client):
    assert uuid.UUID(client.get("/api/v1/campaigns").headers["x-request-id"])
    assert uuid.UUID(anon_client.get("/health").headers["x-request-id"])
    assert uuid.UUID(anon_client.get("/api/v1/campaigns").headers["x-request-id"])  # 401 too


def test_a_safe_caller_supplied_request_id_is_echoed(anon_client):
    response = anon_client.get("/health", headers={"X-Request-ID": "trace-abc_123.xyz"})
    assert response.headers["x-request-id"] == "trace-abc_123.xyz"


@pytest.mark.parametrize(
    "bad",
    ["has space", "semi;colon", "x" * 65, "new\tline", "<script>", "é", "../../etc/passwd", ""],
)
def test_unsafe_caller_supplied_request_ids_are_replaced_not_echoed(anon_client, bad):
    response = anon_client.get("/health", headers={"X-Request-ID": bad.encode("latin-1")})
    returned = response.headers["x-request-id"]
    assert returned != bad
    assert uuid.UUID(returned)


def test_request_id_is_stamped_on_log_records(anon_client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.security"):
        response = anon_client.post(_LOGIN, json={"username": "x", "password": "y"})
    records = [r for r in caplog.records if r.message == "security_event"]
    assert records and records[0].request_id == response.headers["x-request-id"]


def test_request_id_does_not_leak_between_requests(anon_client):
    first = anon_client.get("/health").headers["x-request-id"]
    second = anon_client.get("/health").headers["x-request-id"]
    assert first != second
    assert request_context.current_request_id() == "-"  # context reset after the request


def test_cors_preflight_and_413_also_carry_request_ids(anon_client):
    origin = get_settings().admin_cors_origins[0]
    preflight = anon_client.options(
        "/api/v1/admin/auth/login",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert "x-request-id" in preflight.headers
    big = anon_client.post(_LOGIN, content=b"x" * 2_000_000)
    assert big.status_code == 413 and "x-request-id" in big.headers


# --- robustness of the audit path itself ---------------------------------------------------------


def test_an_audit_write_failure_does_not_turn_a_403_into_a_500(
    operator_client, monkeypatch, caplog
):
    def boom(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("db down"))

    monkeypatch.setattr(security_audit, "record_audit_event", boom)
    with caplog.at_level(logging.ERROR):
        response = operator_client.post("/api/v1/campaigns", json={"name": "x"})
    assert response.status_code == 403
    assert "security_audit_write_failed" in caplog.text


@pytest.mark.parametrize("key", ["name", "args", "message", "module", "levelname", "msg"])
def test_metadata_keys_that_collide_with_logrecord_attributes_cannot_break_logging(
    db_session, key
):
    """Regression: logging raises KeyError for an `extra` key that shadows a LogRecord
    attribute; that used to turn every denial into a 500."""
    security_audit.record_security_event(
        db_session, action="test.event", actor="a", metadata={key: "value"}
    )
    assert len(_rows(db_session, "test.event")) == 1


def test_throttle_memory_is_bounded(db_session):
    for i in range(security_audit._THROTTLE_MAX_KEYS + 50):
        security_audit._should_write("a", f"key-{i}")
    assert len(security_audit._last_written) <= security_audit._THROTTLE_MAX_KEYS


# --- no secrets / PII in the audit trail ----------------------------------------------------------


def test_no_security_row_contains_credentials_tokens_or_phone_numbers(
    anon_client, operator_client, db_session
):
    settings = get_settings()
    anon_client.post(_LOGIN, json={"username": "u", "password": "WRONGPASSWORD"})
    login = anon_client.post(
        _LOGIN, json={"username": settings.admin_username, "password": settings.admin_password}
    )
    anon_client.post(_DOGRAH, json={}, headers={"Authorization": "Bearer WRONGWEBHOOKSECRET"})
    operator_client.post(
        "/api/v1/contacts",
        json={"campaign_id": str(uuid.uuid4()), "phone_number": "+919845550123"},
    )

    everything = " ".join(
        _blob(r)
        for r in db_session.execute(select(AuditLog)).scalars()
        if r.action.split(".")[0] in {"auth", "authz", "webhook"}
    )
    for forbidden in (
        "WRONGPASSWORD",
        "WRONGWEBHOOKSECRET",
        settings.admin_password,
        settings.dograh_webhook_secret,
        settings.jwt_signing_key,
        login.json()["access_token"],
        "+919845550123",
        "4155550123",
    ):
        assert forbidden not in everything


# --- durability: rows must survive the exception that follows them ------------------------
#
# The shared test session never rolls back, so the tests above cannot prove this. Here the
# REAL get_db runs (it rolls back whenever an exception propagates) and the rows are read
# back over a separate connection, i.e. only what was truly COMMITTED is visible.


@pytest.fixture
def real_db_client():
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.core.database import get_db
    from app.main import app
    from tests.conftest import _bearer, test_engine

    saved = app.dependency_overrides.pop(get_db, None)
    tag = f"persist-{uuid.uuid4().hex[:8]}"
    try:
        yield TestClient(app, headers=_bearer("operator") | {"X-Request-ID": tag}), tag
    finally:
        if saved is not None:
            app.dependency_overrides[get_db] = saved
        with test_engine.begin() as conn:
            conn.execute(
                delete(AuditLog).where(AuditLog.event_metadata["request_id"].astext == tag)
            )


def _committed(tag, action):
    from sqlalchemy.orm import Session

    from tests.conftest import test_engine

    with Session(test_engine) as other_connection:
        return (
            other_connection.execute(
                select(AuditLog).where(
                    AuditLog.action == action, AuditLog.event_metadata["request_id"].astext == tag
                )
            )
            .scalars()
            .all()
        )


def test_role_denial_row_is_committed_despite_the_403(real_db_client):
    http, tag = real_db_client
    assert http.post("/api/v1/campaigns", json={"name": "x"}).status_code == 403
    assert len(_committed(tag, "authz.denied")) == 1


def test_failed_login_row_is_committed_despite_the_401(real_db_client):
    http, tag = real_db_client
    response = http.post(
        _LOGIN, json={"username": "x", "password": "y"}, headers={"X-Request-ID": tag}
    )
    assert response.status_code == 401
    assert len(_committed(tag, "auth.login_failed")) == 1


def test_webhook_auth_failure_row_is_committed_despite_the_401(real_db_client):
    http, tag = real_db_client
    response = http.post(
        _DOGRAH, json={}, headers={"Authorization": "Bearer nope", "X-Request-ID": tag}
    )
    assert response.status_code == 401
    assert len(_committed(tag, "webhook.auth_failed")) == 1


def test_enqueue_blocked_row_is_committed_despite_the_409(real_db_client):
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from tests.conftest import _bearer, test_engine

    http, tag = real_db_client
    with Session(test_engine) as session:
        campaign = Campaign(name="persist-check", status=CampaignStatus.ACTIVE)
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id
    kill_switch.enable("admin", None)
    try:
        response = http.post(
            f"/api/v1/campaigns/{campaign_id}/enqueue",
            headers=_bearer("admin") | {"X-Request-ID": tag},
        )
        assert response.status_code == 409
        assert len(_committed(tag, "campaign.enqueue_blocked")) == 1
    finally:
        kill_switch.disable()
        with test_engine.begin() as conn:
            conn.execute(delete(AuditLog).where(AuditLog.entity_id == campaign_id))
            conn.execute(delete(Campaign).where(Campaign.id == campaign_id))


# --- unauthenticated token failures: log-only, reason codes, no token ---------------------


def test_missing_and_invalid_tokens_are_logged_with_reason_codes_but_not_audited(
    anon_client, db_session, caplog
):
    before = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    with caplog.at_level(logging.WARNING, logger="app.security"):
        anon_client.get("/api/v1/campaigns")  # no header
        anon_client.get("/api/v1/campaigns", headers={"Authorization": "Bearer SECRETTOKEN123"})
    records = [r for r in caplog.records if r.message == "auth_rejected"]
    assert [r.sec_reason for r in records] == [
        "missing_or_malformed_header",
        "invalid_or_expired_token",
    ]
    assert all(r.sec_route == "/api/v1/campaigns" and r.sec_method == "GET" for r in records)
    assert "SECRETTOKEN123" not in caplog.text  # never the credential
    after = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert after == before  # log-only: an unauthenticated flood cannot write DB rows


def test_successful_authentication_logs_nothing(client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.security"):
        client.get("/api/v1/campaigns")
    assert [r for r in caplog.records if r.message == "auth_rejected"] == []
