"""CP11 -- Unicode / malformed-input handling. Valid Unicode is processed
normally, bad business input is a 4xx, and nothing here may be a 5xx."""

import pytest

from app.core.config import get_settings

_UNICODE_SAMPLES = {
    "hindi": "रोशन कुमार",
    "bengali": "বাংলা অভিযান",
    "arabic": "حملة اختبار",
    "emoji": "launch 🚀😀",
    "punctuation": "“quoted” — it’s… ok",
    "combining": "e\u0301 n\u0303 a\u0308",  # decomposed accents
    "zero-width": "a\u200bb\u200dc",
}
_LONE_SURROGATE = '"\\ud800"'  # valid JSON text, not encodable as UTF-8


@pytest.mark.parametrize("text", _UNICODE_SAMPLES.values(), ids=_UNICODE_SAMPLES.keys())
def test_login_with_unicode_credentials_is_401_not_500(anon_client, text):
    for body in ({"username": text, "password": "x"}, {"username": "admin", "password": text}):
        response = anon_client.post("/api/v1/admin/auth/login", json=body)
        assert response.status_code == 401, body


def test_login_with_lone_surrogate_is_422_not_500(anon_client):
    raw = '{"username": ' + _LONE_SURROGATE + ', "password": ' + _LONE_SURROGATE + "}"
    response = anon_client.post(
        "/api/v1/admin/auth/login", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_login_succeeds_when_the_configured_secret_is_non_ascii(anon_client, monkeypatch):
    """The fix must compare correctly, not just stop crashing: a legitimate
    non-ASCII password still logs in, a near-miss does not."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_password", "पासवर्ड😀é")
    ok = anon_client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": "पासवर्ड😀é"},
    )
    assert ok.status_code == 200
    near = anon_client.post(
        "/api/v1/admin/auth/login",
        json={"username": settings.admin_username, "password": "पासवर्ड😀e"},
    )
    assert near.status_code == 401


def test_login_rejects_oversized_credentials(anon_client):
    response = anon_client.post(
        "/api/v1/admin/auth/login", json={"username": "a" * 257, "password": "x"}
    )
    assert response.status_code == 422


def test_non_ascii_authorization_header_is_401(anon_client):
    response = anon_client.get(
        "/api/v1/campaigns", headers={"Authorization": "Bearer é".encode("latin-1")}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("text", _UNICODE_SAMPLES.values(), ids=_UNICODE_SAMPLES.keys())
def test_unicode_campaign_name_round_trips(client, text):
    created = client.post("/api/v1/campaigns", json={"name": text})
    assert created.status_code == 201
    fetched = client.get(f"/api/v1/campaigns/{created.json()['id']}")
    assert fetched.json()["name"] == text


def test_unicode_in_csv_import_is_processed(client):
    csv_bytes = "name,phone_number\nरोशन,+919845550100\n".encode("utf-8-sig")
    response = client.post(
        "/api/v1/campaigns/import",
        data={"name": "अभियान 🚀"},
        files={"file": ("contacts.csv", csv_bytes)},
    )
    assert response.status_code == 201
    assert response.json()["created"] == 1


@pytest.mark.parametrize("encoding", ["utf-16", "latin-1"])
def test_non_utf8_csv_is_a_client_error_not_500(client, encoding):
    body = "phone_number\né+919845550100\n".encode(encoding)
    response = client.post(
        "/api/v1/campaigns/import", data={"name": "x"}, files={"file": ("c.csv", body)}
    )
    assert 400 <= response.status_code < 500


def test_malformed_json_is_422_not_500(client):
    response = client.post(
        "/api/v1/campaigns", content=b'{"name": ', headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_invalid_utf8_request_body_is_a_client_error(client):
    response = client.post(
        "/api/v1/campaigns",
        content=b'{"name": "\xff\xfe"}',
        headers={"content-type": "application/json"},
    )
    assert 400 <= response.status_code < 500


def test_error_responses_do_not_leak_internals(client, anon_client):
    bodies = [
        anon_client.post("/api/v1/admin/auth/login", json={"username": "x", "password": "y"}),
        client.post(
            "/api/v1/campaigns", content=b"{", headers={"content-type": "application/json"}
        ),
    ]
    for response in bodies:
        text = response.text.lower()
        for needle in ("traceback", "sqlalchemy", "psycopg", "/home/", "redis://", "postgresql"):
            assert needle not in text


def test_lone_surrogate_in_any_json_string_field_is_422_not_500(client):
    """Regression: FastAPI's default 422 handler echoed the surrogate back and
    crashed while encoding the response -- on every JSON endpoint, not just login."""
    raw = '{"name": ' + _LONE_SURROGATE + "}"
    response = client.post(
        "/api/v1/campaigns", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_validation_errors_do_not_echo_request_input(client):
    secret = "+919845550123-not-a-phone"
    response = client.post(
        "/api/v1/contacts", json={"campaign_id": "not-a-uuid", "phone_number": secret}
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
