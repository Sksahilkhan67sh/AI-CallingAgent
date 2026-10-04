"""Checkpoint 10 -- the provider contract at the HTTP boundary.

Every outcome of a Dograh trigger must land in exactly one DograhErrorCategory,
and an unusable-but-2xx response must never be read as "no call was placed".
Deterministic httpx stubs only: nothing here talks to a real Dograh instance
(LIVE DOGRAH E2E = UNVERIFIED, see docs/CHECKPOINT-10-NOTES.md).
"""

import httpx
import pytest

from app.services.telephony.dograh_client import (
    AMBIGUOUS_CATEGORIES,
    DograhApiError,
    DograhClient,
    DograhErrorCategory,
)

C = DograhErrorCategory
API_KEY = "dg_cp10_not_a_real_key"
TRIGGER_UUID = "22222222-2222-2222-2222-222222222222"


def _client() -> DograhClient:
    return DograhClient(
        base_url="https://dograh.example.invalid", api_key=API_KEY, trigger_uuid=TRIGGER_UUID
    )


def _respond(monkeypatch, response: httpx.Response | Exception) -> None:
    def fake_post(url, *, headers, json, timeout):
        if isinstance(response, Exception):
            raise response
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.post", fake_post)


def _trigger(client: DograhClient):
    return client.trigger_call(
        phone_number="+15550100001", initial_context={"call_attempt_id": "a"}
    )


def test_accepted_trigger_returns_run_id_only(monkeypatch):
    _respond(monkeypatch, httpx.Response(200, json={"status": "initiated", "workflow_run_id": 7}))
    result = _trigger(_client())
    assert result.workflow_run_id == 7  # "initiated" = accepted for dialing, nothing more


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, C.AUTHENTICATION_ERROR),
        (403, C.AUTHENTICATION_ERROR),
        (404, C.PROVIDER_REJECTED),
        (400, C.VALIDATION_ERROR),
        (422, C.VALIDATION_ERROR),
        (429, C.RATE_LIMITED),
        (503, C.PROVIDER_UNAVAILABLE),
        (501, C.PROVIDER_UNAVAILABLE),
        # the request reached Dograh's application tier and may have created a run
        (500, C.AMBIGUOUS_REQUEST),
        (502, C.AMBIGUOUS_REQUEST),
        (504, C.AMBIGUOUS_REQUEST),
    ],
)
def test_http_status_is_categorized(monkeypatch, status, expected):
    _respond(monkeypatch, httpx.Response(status, json={"detail": "nope"}))
    with pytest.raises(DograhApiError) as exc:
        _trigger(_client())
    assert exc.value.category == expected
    assert exc.value.is_ambiguous == (expected in AMBIGUOUS_CATEGORIES)


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ConnectTimeout("t"), C.CONNECTION_ERROR),  # never connected: definitely not sent
        (httpx.ConnectError("refused"), C.CONNECTION_ERROR),
        (httpx.PoolTimeout("pool"), C.CONNECTION_ERROR),
        (httpx.ReadTimeout("slow"), C.AMBIGUOUS_REQUEST),  # may have been processed
        (httpx.WriteTimeout("slow"), C.AMBIGUOUS_REQUEST),
        (httpx.ReadError("reset"), C.AMBIGUOUS_REQUEST),  # response lost after send
        (httpx.RemoteProtocolError("hung up"), C.AMBIGUOUS_REQUEST),
        (httpx.DecodingError("bad body"), C.UNKNOWN_PROVIDER_ERROR),
    ],
)
def test_transport_failure_is_categorized(monkeypatch, error, expected):
    _respond(monkeypatch, error)
    with pytest.raises(DograhApiError) as exc:
        _trigger(_client())
    assert exc.value.category == expected


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>proxy page</html>"),  # not JSON
        httpx.Response(200, json=["workflow_run_id", 1]),  # not an object
        httpx.Response(200, json={"status": "initiated"}),  # no run id
        httpx.Response(200, json={"workflow_run_id": None}),
        httpx.Response(200, json={"workflow_run_id": "abc"}),
        httpx.Response(200, json={"workflow_run_id": True}),  # bool is an int in Python
        httpx.Response(200, json={"workflow_run_id": 0}),
        httpx.Response(200, json={"workflow_run_id": -5}),
    ],
)
def test_accepted_but_unusable_response_is_ambiguous_not_failed(monkeypatch, response):
    """Dograh said 2xx, so a run may exist: the worker must reconcile, never retrigger."""
    _respond(monkeypatch, response)
    with pytest.raises(DograhApiError) as exc:
        _trigger(_client())
    assert exc.value.category == C.AMBIGUOUS_REQUEST
    assert exc.value.is_ambiguous


def test_provider_error_text_is_bounded_and_carries_no_credential(monkeypatch):
    _respond(monkeypatch, httpx.Response(400, json={"detail": "x" * 5000}))
    with pytest.raises(DograhApiError) as exc:
        _trigger(_client())
    assert len(str(exc.value)) < 400
    assert API_KEY not in str(exc.value)
    assert TRIGGER_UUID not in str(exc.value)


def test_every_category_is_reachable_and_distinct():
    # The nine provider-failure categories required by CP10 (configuration
    # errors are DograhConfigurationError, raised before any request exists).
    assert {c.value for c in C} == {
        "timeout",
        "connection_error",
        "authentication_error",
        "validation_error",
        "rate_limited",
        "provider_unavailable",
        "provider_rejected",
        "ambiguous_request",
        "unknown_provider_error",
    }
