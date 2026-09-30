"""Bounded, SSRF-resistant fetch of a provider-supplied URL -- Checkpoint 09.

The transcript URL arrives in a webhook body. Even behind authentication it
must not let an attacker (or a misconfigured workflow) make this server
call internal addresses. Rules:
  - http(s) only, no redirects, hard timeout, hard size cap;
  - the host must resolve only to public addresses, unless the host is
    explicitly listed in DOGRAH_TRANSCRIPT_ALLOWED_HOSTS (e.g. self-hosted
    MinIO on a private network);
  - the URL itself is never logged (presigned URLs embed credentials).
"""

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 2_000_000
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


class UnsafeUrl(Exception):
    pass


def check_url(url: str, allowed_hosts: list[str]) -> str:
    """Returns the host if the URL is safe to fetch; raises UnsafeUrl."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrl("scheme")
    host = parts.hostname.lower()
    if host in (h.lower() for h in allowed_hosts):
        return host
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl("unresolvable") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UnsafeUrl("non-public address")
    return host


def fetch_json(url: str, allowed_hosts: list[str]) -> Any:
    check_url(url, allowed_hosts)
    response = httpx.get(url, timeout=_TIMEOUT, follow_redirects=False)
    response.raise_for_status()
    if len(response.content) > MAX_BYTES:
        raise UnsafeUrl("too large")
    return response.json()
