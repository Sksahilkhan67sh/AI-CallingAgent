"""CP11 -- SSRF-hardened transcript download.

`transcript_url` comes from the Dograh webhook body, so it is attacker-influenced
(anyone holding the webhook secret, or a compromised Dograh, picks the URL).
Everything here exists to make "fetch a URL someone else chose" safe:

* scheme: https only in production (http also allowed in dev/staging);
* no URL credentials; untrusted hosts may only use the scheme's default port;
* every resolved address is checked. Loopback / link-local (cloud metadata) /
  unspecified / multicast / reserved are blocked for everyone. Any other
  non-public address (RFC1918, ULA, CGNAT 100.64/10, documentation ranges...)
  is allowed ONLY for Dograh's own host or DOGRAH_TRANSCRIPT_EXTRA_HOSTS;
* IPv4-mapped IPv6 (::ffff:a.b.c.d) is judged by the IPv4 it embeds;
* DNS rebinding is closed, not just documented: the name is resolved ONCE, all
  addresses validated, and the connection is made to that validated IP with the
  original Host header / TLS SNI + certificate hostname preserved. There is no
  second lookup for an attacker to answer differently. `trust_env=False` stops
  an HTTP(S)_PROXY environment variable from re-routing (and re-resolving) it;
* redirects are NEVER followed (a 3xx is a failed fetch, nothing is requested);
* connect/read timeouts, a total deadline, a decoded-size cap and a
  content-type allow-list, streamed so the body is never unbounded in memory.

Residual risk: a TRUSTED host (Dograh's own) is trusted with its private
address by design; if that host is itself compromised it can serve any content
within the caps above. A path on a trusted host could also reach an internal
service behind it -- keep DOGRAH_TRANSCRIPT_EXTRA_HOSTS minimal.
"""

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"https": 443, "http": 80}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_TOTAL_DEADLINE_SECONDS = 20.0
# Plausible content types for a JSON export. text/plain and octet-stream are
# included because object stores commonly serve JSON that way; HTML, images etc.
# (an error page, a redirect interstitial, a payload that is not a transcript) are not.
_ALLOWED_CONTENT_TYPES = frozenset(
    {"application/json", "text/json", "text/plain", "application/octet-stream"}
)


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    ip: str
    path_and_query: str


def _allowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, trusted: bool) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    ):
        return False
    return trusted or ip.is_global


def _trusted_hosts() -> set[str | None]:
    settings = get_settings()
    return {
        urlsplit(settings.dograh_api_base_url).hostname,
        *settings.dograh_transcript_extra_hosts,
    }


def resolve_target(url: str) -> _Target | None:
    """Validate `url` and return the pinned connection target, or None if it must
    not be fetched. Never raises."""
    settings = get_settings()
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    allowed_schemes = {"https"} if settings.environment == "production" else {"https", "http"}
    scheme = parts.scheme.lower()
    if scheme not in allowed_schemes or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    trusted = host in _trusted_hosts()
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    elif not trusted and port != _DEFAULT_PORTS[scheme]:
        return None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not addresses or not all(_allowed_ip(a, trusted) for a in addresses):
        return None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return _Target(scheme, host, port, str(addresses[0]), path)


def _is_json_content_type(header: str | None) -> bool:
    media_type = (header or "").split(";")[0].strip().lower()
    return media_type in _ALLOWED_CONTENT_TYPES or media_type.endswith("+json")


def _download(target: _Target) -> bytes | None:
    settings = get_settings()
    max_bytes = settings.dograh_transcript_max_bytes
    ip_literal = f"[{target.ip}]" if ":" in target.ip else target.ip
    default_port = _DEFAULT_PORTS[target.scheme]
    host_header = target.host if target.port == default_port else f"{target.host}:{target.port}"
    connect_url = f"{target.scheme}://{ip_literal}:{target.port}{target.path_and_query}"
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS

    with (
        httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client,
        client.stream(
            "GET", connect_url, headers={"Host": host_header}, extensions=extensions
        ) as response,
    ):
        if response.status_code != 200:  # includes every 3xx: redirects are not followed
            logger.warning("dograh_transcript_fetch_rejected", extra={"reason": "status"})
            return None
        if not _is_json_content_type(response.headers.get("content-type")):
            logger.warning("dograh_transcript_fetch_rejected", extra={"reason": "content_type"})
            return None
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            logger.warning("dograh_transcript_fetch_rejected", extra={"reason": "too_large"})
            return None
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                logger.warning("dograh_transcript_fetch_rejected", extra={"reason": "too_large"})
                return None
            if time.monotonic() > deadline:
                logger.warning("dograh_transcript_fetch_rejected", extra={"reason": "deadline"})
                return None
        return bytes(body)


def fetch_transcript_lines(url: str) -> list[dict]:
    """Best-effort: never raises (a failed transcript must not fail webhook
    processing -- the call's terminal-state transition is the durable part) and
    never logs the URL (it may carry a signed token)."""
    target = resolve_target(url)
    if target is None:
        logger.warning("dograh_transcript_url_blocked")
        return []
    try:
        body = _download(target)
        data = json.loads(body) if body is not None else None
    except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL, OSError):
        logger.warning("dograh_transcript_fetch_failed")
        return []
    except (ValueError, RecursionError):  # not JSON / not UTF-8 / absurdly nested
        logger.warning("dograh_transcript_parse_failed")
        return []
    if not isinstance(data, list):
        return []
    lines = [item for item in data if isinstance(item, dict)]
    return lines[: get_settings().dograh_transcript_max_lines]
