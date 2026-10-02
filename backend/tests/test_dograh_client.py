"""Checkpoint 08 -- DograhClient.trigger_call against Dograh's
documented API Trigger contract."""

import httpx
import pytest

from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhClient,
    DograhConfigurationError,
    DograhErrorCategory,
)


def _client(**overrides):
    defaults = dict(
        base_url="https://dograh.example.com",
        api_key="dg_test_key",
        trigger_uuid="11111111-1111-1111-1111-111111111111",
        mode="test",
    )
    defaults.update(overrides)
    return DograhClient(**defaults)


def test_missing_api_key_raises_configuration_error():
    with pytest.raises(DograhConfigurationError):
        DograhClient(
            base_url="https://dograh.example.com",
            api_key="",
            trigger_uuid="11111111-1111-1111-1111-111111111111",
        )


def test_missing_trigger_uuid_raises_configuration_error():
    with pytest.raises(DograhConfigurationError):
        DograhClient(base_url="https://dograh.example.com", api_key="dg_test", trigger_uuid="")


def test_test_mode_uses_the_test_trigger_path():
    client = _client(mode="test")
    expected = "/api/v1/public/agent/test/11111111-1111-1111-1111-111111111111"
    assert client._trigger_path() == expected


def test_production_mode_uses_the_production_trigger_path():
    client = _client(mode="production")
    assert client._trigger_path() == "/api/v1/public/agent/11111111-1111-1111-1111-111111111111"


def test_successful_trigger_returns_workflow_run_info(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(
            200,
            json={"status": "initiated", "workflow_run_id": 12345, "workflow_run_name": "WR-7823"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    client = _client()
    result = client.trigger_call(
        phone_number="+15551234567", initial_context={"call_attempt_id": "abc"}
    )

    assert result.workflow_run_id == 12345
    assert result.workflow_run_name == "WR-7823"
    assert captured["headers"]["X-API-Key"] == "dg_test_key"
    assert captured["json"]["phone_number"] == "+15551234567"
    assert captured["json"]["initial_context"] == {"call_attempt_id": "abc"}
    assert captured["url"].endswith(
        "/api/v1/public/agent/test/11111111-1111-1111-1111-111111111111"
    )


def test_400_response_raises_dograh_api_error_with_detail(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(
            400,
            json={"detail": "Telephony not configured for this organization"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    client = _client()
    with pytest.raises(DograhApiError) as exc_info:
        client.trigger_call(phone_number="+15551234567", initial_context={})

    assert exc_info.value.status_code == 400
    assert "Telephony not configured" in exc_info.value.message
    assert exc_info.value.category == DograhErrorCategory.VALIDATION_ERROR


def test_401_response_raises_dograh_api_error(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(
            401, json={"detail": "Invalid API key"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.status_code == 401
    assert exc_info.value.category == DograhErrorCategory.AUTHENTICATION_ERROR


def test_generic_timeout_raises_ambiguous_dograh_api_error(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.TIMEOUT
    assert exc_info.value.is_ambiguous is True


def test_read_timeout_is_ambiguous_request(monkeypatch):
    """§1.3: the request may have reached Dograh before we lost the
    response -- must be classified as ambiguous, never a confirmed
    failure that gets blindly retried."""

    def fake_post(url, *, headers, json, timeout):
        raise httpx.ReadTimeout("no response")

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.AMBIGUOUS_REQUEST
    assert exc_info.value.is_ambiguous is True


def test_connect_timeout_is_not_ambiguous(monkeypatch):
    """A connection that was never established means the request
    definitely never reached Dograh -- safe to treat as a plain
    connection error, not ambiguous."""

    def fake_post(url, *, headers, json, timeout):
        raise httpx.ConnectTimeout("could not connect")

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.CONNECTION_ERROR
    assert exc_info.value.is_ambiguous is False


def test_connect_error_is_not_ambiguous(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.CONNECTION_ERROR
    assert exc_info.value.is_ambiguous is False


def test_rate_limited_status_is_classified(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(
            429, json={"detail": "slow down"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.RATE_LIMITED


def test_5xx_status_is_provider_unavailable(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(503, text="down", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.PROVIDER_UNAVAILABLE


def test_404_status_is_provider_rejected(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return httpx.Response(
            404, json={"detail": "trigger not found"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)

    with pytest.raises(DograhApiError) as exc_info:
        _client().trigger_call(phone_number="+15551234567", initial_context={})
    assert exc_info.value.category == DograhErrorCategory.PROVIDER_REJECTED
