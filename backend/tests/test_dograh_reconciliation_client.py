"""Phase 1 -- DograhClient.find_runs_for_attempt against Dograh's DOCUMENTED
org-wide run listing (GET /api/v1/organizations/usage/runs). Verified against
the published OpenAPI only, not a live instance."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhClient,
    DograhErrorCategory,
)

ATTEMPT = "11111111-2222-3333-4444-555555555555"
SINCE = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)


def _client() -> DograhClient:
    return DograhClient(
        base_url="https://dograh.example.com",
        api_key="dg_test_key",
        trigger_uuid="22222222-2222-2222-2222-222222222222",
        mode="production",
    )


def _page(runs, *, page=1, total_pages=1):
    return {"runs": runs, "page": page, "total_pages": total_pages, "total_count": len(runs)}


def _run(run_id, attempt=None):
    return {"id": run_id, "initial_context": {"call_attempt_id": attempt} if attempt else None}


def _serve(monkeypatch, pages):
    """pages: list of JSON bodies served in order; returns the captured calls."""
    calls = []

    def fake_get(url, *, headers, params, timeout):
        calls.append({"url": url, "headers": headers, "params": params})
        body = pages[len(calls) - 1]
        if isinstance(body, Exception):
            raise body
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr("app.services.telephony.dograh_client.httpx.get", fake_get)
    return calls


def test_single_match_is_returned_and_request_is_bounded(monkeypatch):
    calls = _serve(monkeypatch, [_page([_run(1, "other"), _run(2, ATTEMPT), _run(3)])])

    assert _client().find_runs_for_attempt(ATTEMPT, SINCE) == [2]

    (call,) = calls
    assert call["url"] == "https://dograh.example.com/api/v1/organizations/usage/runs"
    assert call["headers"] == {"X-API-Key": "dg_test_key"}
    assert call["params"]["limit"] == 100 and call["params"]["page"] == 1
    # window starts a little BEFORE the attempt (clock skew) and ends now
    assert call["params"]["start_date"] == (SINCE - timedelta(seconds=60)).isoformat()
    assert datetime.fromisoformat(call["params"]["end_date"]) <= datetime.now(UTC)


def test_no_match_after_reading_the_whole_window_returns_empty(monkeypatch):
    _serve(monkeypatch, [_page([_run(1, "other")])])
    assert _client().find_runs_for_attempt(ATTEMPT, SINCE) == []


def test_matches_are_collected_across_pages_and_deduplicated(monkeypatch):
    calls = _serve(
        monkeypatch,
        [
            _page([_run(5, ATTEMPT)], page=1, total_pages=3),
            _page([_run(6, "x")], page=2, total_pages=3),
            _page([_run(7, ATTEMPT), _run(5, ATTEMPT)], page=3, total_pages=3),
        ],
    )
    assert _client().find_runs_for_attempt(ATTEMPT, SINCE) == [5, 7]
    assert [c["params"]["page"] for c in calls] == [1, 2, 3]


def test_window_larger_than_the_page_bound_is_inconclusive_not_empty(monkeypatch):
    """Never report 'no run' when part of the window was not read."""
    _serve(monkeypatch, [_page([], page=i, total_pages=50) for i in range(1, 6)])
    with pytest.raises(DograhApiError) as exc:
        _client().find_runs_for_attempt(ATTEMPT, SINCE)
    assert exc.value.category == DograhErrorCategory.UNKNOWN_PROVIDER_ERROR


@pytest.mark.parametrize(
    "body",
    [{"unexpected": True}, {"runs": "nope", "total_pages": 1}, {"runs": [{}], "total_pages": "x"}],
)
def test_malformed_response_is_an_error_not_a_silent_empty_result(monkeypatch, body):
    _serve(monkeypatch, [body])
    with pytest.raises(DograhApiError):
        _client().find_runs_for_attempt(ATTEMPT, SINCE)


def test_provider_error_status_is_a_taxonomy_error(monkeypatch):
    _serve(
        monkeypatch,
        [httpx.Response(503, json={"detail": "down"}, request=httpx.Request("GET", "u"))],
    )
    with pytest.raises(DograhApiError) as exc:
        _client().find_runs_for_attempt(ATTEMPT, SINCE)
    assert exc.value.status_code == 503


def test_timeout_is_a_taxonomy_error(monkeypatch):
    _serve(monkeypatch, [httpx.ReadTimeout("slow")])
    with pytest.raises(DograhApiError):
        _client().find_runs_for_attempt(ATTEMPT, SINCE)
