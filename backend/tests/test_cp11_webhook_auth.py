"""CP11 -- legacy telephony webhook authentication ordering.

(The Dograh webhook's equivalents already live in test_dograh_webhook_hardening.py.)"""

from app.core.config import get_settings

_URL = "/api/v1/webhooks/telephony/call-status"
_VALID = {"event_id": "e1", "provider_call_id": "x", "status": "connected"}


def test_missing_secret_is_401_not_422(anon_client):
    assert anon_client.post(_URL, json=_VALID).status_code == 401


def test_wrong_secret_is_401(anon_client):
    response = anon_client.post(_URL, json=_VALID, headers={"X-Webhook-Secret": "nope"})
    assert response.status_code == 401


def test_non_ascii_secret_is_401_not_500(anon_client):
    response = anon_client.post(
        _URL, json=_VALID, headers={"X-Webhook-Secret": "é".encode("latin-1")}
    )
    assert response.status_code == 401


def test_unauthenticated_caller_gets_no_schema_feedback(anon_client):
    """Auth runs before body validation: a bad secret with a garbage body is still 401."""
    for body in ({}, {"unexpected": 1}, {"status": "not-a-status"}):
        response = anon_client.post(_URL, json=body, headers={"X-Webhook-Secret": "nope"})
        assert response.status_code == 401, body
        assert response.json() == {"detail": "Invalid webhook signature"}


def test_correct_secret_reaches_processing(anon_client):
    response = anon_client.post(
        _URL, json=_VALID, headers={"X-Webhook-Secret": get_settings().telephony_webhook_secret}
    )
    assert response.status_code not in (401, 422)  # unknown call id -> 404 from the service
