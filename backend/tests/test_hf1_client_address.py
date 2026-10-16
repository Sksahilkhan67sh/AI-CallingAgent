"""HF1 1.2 -- ONE shared client-address helper, honoring X-Forwarded-For only when the
socket peer is a configured trusted proxy (TRUSTED_PROXIES), taking the RIGHT-MOST entry
that is not itself a trusted proxy. Default (empty) = use the socket peer, header ignored.
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.core.client_address import client_address, parse_trusted_proxies, rate_limit_identity
from app.core.config import Settings, get_settings
from app.main import app

_LOGIN = "/api/v1/admin/auth/login"
_DOGRAH = "/api/v1/webhooks/dograh/call-completed"


def _request(peer: str | None, *xff: str, extra_headers=None) -> Request:
    headers = [(b"x-forwarded-for", value.encode("utf-8", "replace")) for value in xff]
    headers += [(k.encode(), v.encode()) for k, v in (extra_headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "client": (peer, 5000) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def trust(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "trusted_proxies", value)

    return _set


# --- default: header never honored --------------------------------------------------


def test_default_uses_the_socket_peer_and_ignores_the_header():
    assert client_address(_request("203.0.113.7", "1.2.3.4")) == "203.0.113.7"


def test_no_client_at_all_is_unknown_not_an_error():
    assert client_address(_request(None, "1.2.3.4")) == "unknown"


def test_spoofed_header_from_an_untrusted_peer_is_ignored(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("203.0.113.7", "9.9.9.9")) == "203.0.113.7"


# --- trusted peer: right-most non-trusted entry ----------------------------------------


def test_trusted_peer_yields_the_forwarded_client(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2", "198.51.100.7")) == "198.51.100.7"


def test_forged_left_entries_are_ignored_rightmost_untrusted_wins(trust):
    trust("10.0.0.2/32")
    # the client sent "1.1.1.1"; our proxy appended the real address it saw
    assert client_address(_request("10.0.0.2", "1.1.1.1, 203.0.113.9")) == "203.0.113.9"


def test_multi_hop_chain_skips_every_trusted_hop(trust):
    trust("10.0.0.0/8, 192.168.0.0/16")
    chain = "203.0.113.50, 198.51.100.7, 192.168.1.5, 10.1.1.1"
    assert client_address(_request("10.0.0.2", chain)) == "198.51.100.7"


def test_multiple_header_lines_are_treated_as_one_comma_joined_list(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2", "203.0.113.50", "198.51.100.7")) == "198.51.100.7"


def test_all_entries_trusted_falls_back_to_the_peer(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2", "10.0.0.9, 10.0.0.8")) == "10.0.0.2"


def test_trusted_peer_without_a_header_is_the_peer(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2")) == "10.0.0.2"


# --- IPv6 -----------------------------------------------------------------------------


def test_ipv6_peer_and_client(trust):
    trust("fd00::/8")
    assert client_address(_request("fd00::1", "2001:db8::77")) == "2001:db8::77"


def test_ipv6_addresses_are_normalised(trust):
    trust("fd00::/8")
    assert client_address(_request("fd00::1", "2001:0DB8:0000:0000:0000:0000:0000:0077")) == (
        "2001:db8::77"
    )


def test_ipv4_mapped_ipv6_is_the_same_address_as_ipv4(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("::ffff:10.0.0.2", "::ffff:198.51.100.7")) == "198.51.100.7"


def test_a_port_or_brackets_on_a_forwarded_entry_is_tolerated(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2", "198.51.100.7:51234")) == "198.51.100.7"
    assert client_address(_request("10.0.0.2", "[2001:db8::7]:443")) == "2001:db8::7"


def test_ipv6_rate_limit_identity_groups_a_whole_slash_64():
    """One host owns 2**64 addresses; a per-address limiter would be trivially bypassed."""
    a = rate_limit_identity(_request("2001:db8:1:2:aaaa::1"))
    b = rate_limit_identity(_request("2001:db8:1:2:bbbb::9"))
    c = rate_limit_identity(_request("2001:db8:1:3::1"))
    assert a == b and a != c


# --- malformed input never raises -----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        ",,,",
        "not-an-ip",
        "999.999.999.999",
        "1.2.3.4, garbage",
        "garbage, 1.2.3.4",
        "\x00\x01",
        "é, ü",
        "a" * 10_000,
        ", ".join(["1.2.3.4"] * 5000),
        "[[[",
        "::::",
    ],
)
def test_malformed_forwarded_for_never_raises_and_never_trusts_garbage(trust, value):
    trust("10.0.0.0/8")
    result = client_address(_request("10.0.0.2", value))
    assert isinstance(result, str) and result
    assert result != "garbage"


def test_a_malformed_right_most_entry_falls_back_to_the_peer(trust):
    trust("10.0.0.0/8")
    assert client_address(_request("10.0.0.2", "198.51.100.7, garbage")) == "10.0.0.2"


def test_non_ip_peer_is_never_treated_as_trusted(trust):
    trust("0.0.0.0/0, ::/0")
    assert client_address(_request("testclient", "1.2.3.4")) == "testclient"


# --- configuration ---------------------------------------------------------------------


def test_parse_trusted_proxies_accepts_spaces_blanks_and_bare_addresses():
    nets = parse_trusted_proxies(" 10.0.0.0/8 , ,192.168.1.1, fd00::/8 ")
    assert [str(n) for n in nets] == ["10.0.0.0/8", "192.168.1.1/32", "fd00::/8"]
    assert parse_trusted_proxies("") == [] and parse_trusted_proxies("  ") == []


@pytest.mark.parametrize("bad", ["10.0.0.0/33", "not-a-cidr", "10.0.0.0/8, nope", "1.2.3.4/abc"])
def test_malformed_cidr_is_a_startup_error(bad):
    with pytest.raises(ValidationError):
        Settings(trusted_proxies=bad)


def test_settings_accept_a_valid_list():
    assert Settings(trusted_proxies="10.0.0.0/8,fd00::/8").trusted_proxies == "10.0.0.0/8,fd00::/8"


def test_the_dockerfile_does_not_trust_forwarded_headers_blindly():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "--forwarded-allow-ips" not in dockerfile
    assert "--proxy-headers" not in dockerfile


# --- every limiter and audit event uses the helper -------------------------------------


class _PeerAddress:
    def __init__(self, inner, addr):
        self._inner, self._addr = inner, addr

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self._addr, 40000)}
        await self._inner(scope, receive, send)


def test_login_limiter_buckets_by_forwarded_client_only_behind_a_trusted_proxy(
    client, trust, monkeypatch
):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 1)
    bad = {"username": "someone", "password": "x"}
    proxy = TestClient(_PeerAddress(app, "10.0.0.2"))

    # untrusted proxy: header is ignored, so everyone behind it shares ONE bucket
    codes = [
        proxy.post(_LOGIN, json=bad, headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
        for i in range(1, 4)
    ]
    assert codes == [401, 429, 429]

    # trusted proxy: each real client gets its own bucket
    trust("10.0.0.0/8")
    codes = [
        proxy.post(_LOGIN, json=bad, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
        for i in range(1, 4)
    ]
    assert codes == [401, 401, 401]


def test_webhook_failure_audit_records_the_forwarded_client_address(
    client, trust, db_session
):
    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    trust("10.0.0.0/8")
    proxy = TestClient(_PeerAddress(app, "10.0.0.2"))
    proxy.post(
        _DOGRAH,
        json={"call_attempt_id": str(uuid.uuid4()), "call_status": "user_hangup"},
        headers={"X-Forwarded-For": "198.51.100.77"},
    )
    rows = db_session.execute(
        select(AuditLog).where(AuditLog.action == "webhook.auth_failed")
    ).scalars().all()
    assert rows and rows[-1].event_metadata["source_ip"] == "198.51.100.77"
