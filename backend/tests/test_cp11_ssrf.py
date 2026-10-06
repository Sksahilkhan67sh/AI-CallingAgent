"""CP11 -- transcript URL SSRF hardening (app/services/telephony/transcript_fetch.py).

The code under test is the real validation + fetch path. Only two seams are
faked: DNS (so hostnames can "resolve" to chosen addresses) and the HTTP
transport (so we can see exactly what would be sent, and to which IP).
"""

import json
import logging
import socket

import httpx
import pytest

from app.core.config import get_settings
from app.services.telephony import transcript_fetch as tf
from app.services.telephony.dograh_webhook_service import _is_safe_transcript_url

PUBLIC_IP = "93.184.216.34"
_LINES = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]


def _fake_dns(monkeypatch, mapping):
    """host -> address (or list of addresses). Unknown hosts fail to resolve."""

    def fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror("no such host")
        addrs = mapping[host] if isinstance(mapping[host], list) else [mapping[host]]
        family = lambda a: socket.AF_INET6 if ":" in a else socket.AF_INET  # noqa: E731
        return [(family(a), 0, 0, "", (a, port)) for a in addrs]

    monkeypatch.setattr(tf.socket, "getaddrinfo", fake)


@pytest.fixture
def wire(monkeypatch):
    """Route the fetch through a MockTransport. `wire.handler` decides the
    response; `wire.requests` records every request that would be sent."""

    class Wire:
        requests: list[httpx.Request] = []
        handler = staticmethod(
            lambda request: httpx.Response(
                200, json=_LINES, headers={"content-type": "application/json"}
            )
        )

    wire = Wire()
    wire.requests = []
    real_client = httpx.Client

    def record(request):
        wire.requests.append(request)
        return wire.handler(request)

    monkeypatch.setattr(
        tf.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(record), **kw),
    )
    return wire


def _env(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- destination validation --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/t.json",
        "http://127.1.2.3/t.json",
        "http://localhost/t.json",
        "http://[::1]/t.json",
        "http://0.0.0.0/t.json",
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure metadata
        "http://[fd00:ec2::254]/latest/meta-data/",  # AWS IPv6 metadata
        "http://10.0.0.5/t.json",
        "http://172.16.9.9/t.json",
        "http://192.168.1.1/t.json",
        "http://[fc00::1]/t.json",  # IPv6 unique-local
        "http://[fe80::1]/t.json",  # IPv6 link-local
        "http://100.64.0.1/t.json",  # CGNAT -- the gap the CP10 guard missed
        "http://100.127.255.254/t.json",
        "http://[::ffff:127.0.0.1]/t.json",  # IPv4-mapped loopback
        "http://[::ffff:169.254.169.254]/t.json",  # IPv4-mapped metadata
        "http://[::ffff:10.0.0.1]/t.json",  # IPv4-mapped private
        "http://192.0.2.1/t.json",  # documentation range, not globally routable
        "https:///no-host",
    ],
)
def test_internal_destinations_are_blocked(url):
    assert _is_safe_transcript_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/t.json",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "//example.com/t.json",
        "example.com/t.json",
        "",
    ],
)
def test_unsupported_schemes_and_shapes_are_blocked(url, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    assert _is_safe_transcript_url(url) is False


def test_url_credentials_are_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    assert _is_safe_transcript_url("https://user:pass@example.com/t.json") is False
    assert _is_safe_transcript_url("https://trusted.internal@example.com/t.json") is False


@pytest.mark.parametrize("port", [22, 25, 6379, 5432, 8080, 8443, 9200])
def test_non_default_ports_are_blocked_for_untrusted_hosts(port, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    assert _is_safe_transcript_url(f"https://example.com:{port}/t.json") is False
    assert _is_safe_transcript_url("https://example.com:443/t.json") is True
    assert _is_safe_transcript_url("https://example.com/t.json") is True


def test_invalid_port_does_not_crash():
    assert _is_safe_transcript_url("https://example.com:99999/t.json") is False
    assert _is_safe_transcript_url("https://example.com:abc/t.json") is False


def test_hostname_resolving_to_a_private_address_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"evil.example": "10.0.0.7"})
    assert _is_safe_transcript_url("https://evil.example/t.json") is False


@pytest.mark.parametrize("bad", ["127.0.0.1", "169.254.169.254", "::1", "100.64.0.9"])
def test_any_bad_address_among_several_blocks_the_host(bad, monkeypatch):
    """Multi-A-record rebinding trick: one public record, one internal."""
    _fake_dns(monkeypatch, {"mixed.example": [PUBLIC_IP, bad]})
    assert _is_safe_transcript_url("https://mixed.example/t.json") is False


def test_unresolvable_host_is_blocked(monkeypatch):
    _fake_dns(monkeypatch, {})
    assert _is_safe_transcript_url("https://nxdomain.example/t.json") is False


def test_production_requires_https(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    assert _is_safe_transcript_url("http://example.com/t.json") is True  # development
    # (Booting a full production Settings needs every secret set -- see the production
    # validator tests; here only the environment flag matters.)
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert _is_safe_transcript_url("http://example.com/t.json") is False
    assert _is_safe_transcript_url("https://example.com/t.json") is True


# --- trusted hosts (CP10 behaviour preserved) ------------------------------------------


def test_trusted_host_may_use_private_address_and_custom_port(monkeypatch):
    _env(monkeypatch, DOGRAH_API_BASE_URL="http://dograh.internal:8000")
    _fake_dns(monkeypatch, {"dograh.internal": "10.9.9.9", "other.internal": "10.9.9.10"})
    assert _is_safe_transcript_url("http://dograh.internal:9000/t.json") is True
    assert _is_safe_transcript_url("http://other.internal/t.json") is False


def test_extra_hosts_are_trusted_but_still_never_loopback_or_metadata(monkeypatch):
    _env(monkeypatch, DOGRAH_TRANSCRIPT_EXTRA_HOSTS='["files.internal", "sneaky.internal"]')
    _fake_dns(
        monkeypatch,
        {
            "files.internal": "10.5.5.5",
            "sneaky.internal": "127.0.0.1",
            "meta.internal": "169.254.169.254",
        },
    )
    assert _is_safe_transcript_url("https://files.internal/t.json") is True
    assert _is_safe_transcript_url("https://sneaky.internal/t.json") is False  # loopback: never
    assert _is_safe_transcript_url("https://meta.internal/t.json") is False


# --- redirects ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "https://example.com/other.json",  # even a "safe" redirect is not followed
    ],
)
def test_redirects_are_never_followed(location, wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda request: httpx.Response(302, headers={"location": location})

    assert tf.fetch_transcript_lines("https://example.com/t.json") == []
    assert len(wire.requests) == 1  # nothing was requested at the redirect target


# --- DNS rebinding: the connection is pinned to the validated address ----------------


def test_connection_is_pinned_to_the_validated_ip(wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    assert tf.fetch_transcript_lines("https://example.com/path/t.json?sig=abc") == _LINES

    (request,) = wire.requests
    assert request.url.host == PUBLIC_IP  # connect target is the IP, not the name
    assert request.url.path == "/path/t.json" and request.url.query == b"sig=abc"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"  # TLS name check stays


def test_rebinding_between_check_and_fetch_cannot_reach_an_internal_address(wire, monkeypatch):
    """Attacker DNS: public on the first lookup, 127.0.0.1 on any later one. The
    code must do exactly ONE lookup and connect to what it validated."""
    answers = iter([PUBLIC_IP, "127.0.0.1", "127.0.0.1", "127.0.0.1"])
    lookups = []

    def rebinding_dns(host, port, *a, **kw):
        lookups.append(host)
        return [(socket.AF_INET, 0, 0, "", (next(answers), port))]

    monkeypatch.setattr(tf.socket, "getaddrinfo", rebinding_dns)

    assert tf.fetch_transcript_lines("https://rebind.example/t.json") == _LINES
    assert len(lookups) == 1
    assert [r.url.host for r in wire.requests] == [PUBLIC_IP]


def test_non_default_port_is_preserved_in_host_header_for_trusted_host(wire, monkeypatch):
    _env(monkeypatch, DOGRAH_API_BASE_URL="http://dograh.internal:8000")
    _fake_dns(monkeypatch, {"dograh.internal": "10.9.9.9"})
    tf.fetch_transcript_lines("http://dograh.internal:9000/t.json")
    (request,) = wire.requests
    assert (request.url.host, request.url.port) == ("10.9.9.9", 9000)
    assert request.headers["host"] == "dograh.internal:9000"
    assert "sni_hostname" not in request.extensions  # plain http


def test_ipv6_target_is_bracketed(wire, monkeypatch):
    _fake_dns(monkeypatch, {"v6.example": "2606:2800:220:1::1"})
    assert tf.fetch_transcript_lines("https://v6.example/t.json") == _LINES
    assert wire.requests[0].url.host == "2606:2800:220:1::1"


# --- bounded response: size, time, type, status -----------------------------------------


def _json_body(n_lines):
    return json.dumps([{"role": "user", "content": "x" * 10}] * n_lines).encode()


def test_oversized_declared_content_length_is_rejected_without_reading(wire, monkeypatch):
    _env(monkeypatch, DOGRAH_TRANSCRIPT_MAX_BYTES="1000")
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})

    class _MustNotBeRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("body must not be read when Content-Length exceeds the cap")

    wire.handler = lambda r: httpx.Response(
        200,
        headers={"content-type": "application/json", "content-length": "999999999"},
        stream=_MustNotBeRead(),
    )
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


def test_oversized_streamed_body_without_content_length_is_cut_off(wire, monkeypatch):
    _env(monkeypatch, DOGRAH_TRANSCRIPT_MAX_BYTES="5000")
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    chunks_read = []

    def endless():
        for i in range(10_000):  # would be ~10 MB if consumed
            chunks_read.append(i)
            yield b"x" * 1024

    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, content=endless()
    )
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []
    assert len(chunks_read) < 50  # stopped near the cap, did not drain the stream


def test_body_at_the_limit_is_accepted(wire, monkeypatch):
    body = _json_body(3)
    _env(monkeypatch, DOGRAH_TRANSCRIPT_MAX_BYTES=str(len(body)))
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, content=body
    )
    assert len(tf.fetch_transcript_lines("https://example.com/t.json")) == 3


def test_line_count_is_capped(wire, monkeypatch):
    _env(monkeypatch, DOGRAH_TRANSCRIPT_MAX_LINES="4")
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, content=_json_body(50)
    )
    assert len(tf.fetch_transcript_lines("https://example.com/t.json")) == 4


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "image/png", "application/xml", "application/pdf", "", "multipart/form-data"],
)
def test_wrong_content_type_is_rejected(content_type, wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    headers = {"content-type": content_type} if content_type else {}
    wire.handler = lambda r: httpx.Response(200, headers=headers, content=_json_body(2))
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "APPLICATION/JSON",
        "application/vnd.api+json",
        "text/plain",
        "application/octet-stream",
    ],
)
def test_json_like_content_types_are_accepted(content_type, wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": content_type}, content=_json_body(2)
    )
    assert len(tf.fetch_transcript_lines("https://example.com/t.json")) == 2


@pytest.mark.parametrize("status_code", [204, 301, 400, 403, 404, 500, 503])
def test_non_200_is_a_failed_fetch(status_code, wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(status_code, content=_json_body(2))
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c"), OSError("x")],
)
def test_timeouts_and_network_errors_never_raise(exc, wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})

    def boom(request):
        raise exc

    wire.handler = boom
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


def test_total_deadline_stops_a_slow_drip(wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    clock = iter([0.0, 0.0] + [100.0] * 50)  # deadline computed at t=0, then "20s+ later"
    monkeypatch.setattr(tf.time, "monotonic", lambda: next(clock))
    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, content=iter([b"[", b"{}", b",{}", b"]"])
    )
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"\xff\xfe\x00", b"[" * 100_000, b'"just a string"', b"{}", b"null", b"[1,2,3]"],
)
def test_malformed_or_wrong_shaped_bodies_never_raise(body, wire, monkeypatch):
    """Includes 100k nested brackets (json.loads -> RecursionError) and non-UTF-8."""
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, content=body
    )
    assert tf.fetch_transcript_lines("https://example.com/t.json") == []


def test_non_dict_items_are_filtered(wire, monkeypatch):
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'[{"a":1}, 5, "x", null, {"b":2}]',
    )
    assert tf.fetch_transcript_lines("https://example.com/t.json") == [{"a": 1}, {"b": 2}]


# --- no proxy bypass, no URL in logs --------------------------------------------------


def test_http_proxy_environment_variable_cannot_reroute_the_fetch(monkeypatch):
    seen = {}
    real_client = httpx.Client

    def spy(**kwargs):
        seen.update(kwargs)
        return real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kwargs
        )

    monkeypatch.setattr(tf.httpx, "Client", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker-proxy:3128")
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    tf.fetch_transcript_lines("https://example.com/t.json")
    assert seen["trust_env"] is False and seen["follow_redirects"] is False


def test_signed_url_token_is_never_logged(wire, monkeypatch, caplog):
    # Non-vacuous: the logger must be live (alembic's fileConfig can disable it) and the
    # fetch path must actually have logged something for the absence below to mean anything.
    logging.getLogger(tf.__name__).disabled = False
    _fake_dns(monkeypatch, {"example.com": PUBLIC_IP})
    wire.handler = lambda r: httpx.Response(500)
    with caplog.at_level("DEBUG"):
        tf.fetch_transcript_lines("https://example.com/t.json?X-Amz-Signature=SUPERSECRETTOKEN")
        tf.fetch_transcript_lines("http://127.0.0.1/t.json?X-Amz-Signature=SUPERSECRETTOKEN")
    assert "dograh_transcript_fetch_rejected" in caplog.text  # rejected-by-status was logged
    assert "dograh_transcript_url_blocked" in caplog.text  # blocked-destination was logged
    assert "SUPERSECRETTOKEN" not in caplog.text
    assert "example.com" not in caplog.text


# --- interpreter-independent guarantee for IPv4-mapped IPv6 --------------------------


@pytest.mark.parametrize(
    "mapped", ["::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:0.0.0.0", "::ffff:224.0.0.1"]
)
def test_ipv4_mapped_hard_blocks_apply_even_to_trusted_hosts(mapped):
    """Python 3.11 does not classify ::ffff:127.0.0.1 as loopback (3.12 does), and the
    trusted-host path relies on the hard blocks alone. The code unwraps the embedded
    IPv4 itself so the result does not depend on the interpreter version."""
    import ipaddress

    ip = ipaddress.ip_address(mapped)
    assert tf._allowed_ip(ip, trusted=True) is False
    assert tf._allowed_ip(ip, trusted=False) is False


def test_ipv4_mapped_private_is_allowed_only_for_trusted_hosts():
    import ipaddress

    ip = ipaddress.ip_address("::ffff:10.0.0.1")
    assert tf._allowed_ip(ip, trusted=True) is True
    assert tf._allowed_ip(ip, trusted=False) is False
