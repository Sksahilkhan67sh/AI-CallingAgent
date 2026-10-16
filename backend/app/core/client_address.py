"""HF1 1.2 -- the ONE place that decides "which client address is this request from".

Every per-address limiter and every security audit event uses this helper, so they cannot
disagree.

Behind a reverse proxy the socket peer is the proxy, so a per-address limiter would collapse
into one global bucket. `X-Forwarded-For` fixes that but is caller-controlled, so it is only
honored under two conditions:

  1. the socket peer is inside a configured trusted CIDR (`TRUSTED_PROXIES`), and then
  2. the address taken is the RIGHT-MOST entry that is not itself a trusted proxy -- the
     entries to its left were supplied by the client (or earlier, untrusted hops) and can be
     forged; the entries to its right were appended by proxies we trust.

With `TRUSTED_PROXIES` empty (the default) the header is ignored entirely. This is why the
Dockerfile CMD does NOT use `--proxy-headers --forwarded-allow-ips='*'` (that would make the
header spoofable for every caller).

Nothing here raises on caller-controlled input: a malformed header falls back to the socket
peer rather than to a guess.
"""

import ipaddress
from functools import lru_cache

from fastapi import Request

from app.core.config import get_settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

UNKNOWN = "unknown"

# A forwarded chain longer than this is not a real proxy chain; bound the work an attacker
# can make one request cost.
_MAX_CHAIN_ENTRIES = 64
_MAX_HEADER_CHARS = 4096


def parse_trusted_proxies(raw: str) -> list[Network]:
    """Comma-separated CIDRs (a bare address is a /32 or /128). Blank entries are skipped.
    Raises ValueError naming the offending entry -- used by the Settings validator so a bad
    value is a startup error."""
    networks: list[Network] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"{entry!r} is not a valid CIDR or address") from exc
    return networks


@lru_cache(maxsize=16)
def _trusted(raw: str) -> tuple[Network, ...]:
    try:
        return tuple(parse_trusted_proxies(raw))
    except ValueError:
        # Settings validation rejects this at startup; a value that was patched in later
        # must not widen trust -- trust nothing.
        return ()


def _normalise(address: IPAddress) -> IPAddress:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _parse_ip(text: str) -> IPAddress | None:
    """One header token -> an address, tolerating `host:port` and `[v6]:port`. None if it is
    not an address (never raises)."""
    token = text.strip()
    if not token or len(token) > 64:
        return None
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return None
        token = token[1:end]
    elif token.count(":") == 1:  # IPv4 with a port; a bare IPv6 has several colons
        token = token.split(":", 1)[0]
    try:
        return _normalise(ipaddress.ip_address(token))
    except ValueError:
        return None


def _in_any(address: IPAddress, networks: tuple[Network, ...]) -> bool:
    return any(address.version == net.version and address in net for net in networks)


def _forwarded_entries(request: Request) -> list[str]:
    try:
        joined = ",".join(request.headers.getlist("x-forwarded-for"))
    except Exception:  # noqa: BLE001 -- header access must never fail a request
        return []
    if not joined or len(joined) > _MAX_HEADER_CHARS * 4:
        return []
    return [part for part in joined.split(",")][-_MAX_CHAIN_ENTRIES:]


def client_address(request: Request) -> str:
    """The client's address as a normalised string, or "unknown" when the request has no peer.
    Non-IP peers (for example the test client's placeholder host) are returned unchanged and
    are never treated as trusted proxies."""
    peer_text = request.client.host if request.client else None
    if not peer_text:
        return UNKNOWN
    peer = _parse_ip(peer_text)
    if peer is None:
        return peer_text

    trusted = _trusted(get_settings().trusted_proxies)
    if not trusted or not _in_any(peer, trusted):
        return str(peer)

    for entry in reversed(_forwarded_entries(request)):
        address = _parse_ip(entry)
        if address is None:
            # An unparseable entry where a proxy should have appended one: do not guess, and
            # do not skip past it to a left-hand (client-controlled) value.
            return str(peer)
        if not _in_any(address, trusted):
            return str(address)
    return str(peer)


def rate_limit_identity(request: Request) -> str:
    """Bucket owner for per-address limiters. An IPv6 client owns a whole /64, so it is keyed
    by that prefix -- otherwise rotating the low 64 bits would reset any per-address budget."""
    text = client_address(request)
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return f"ip:{text}"
    if isinstance(address, ipaddress.IPv6Address):
        return f"ip:{ipaddress.ip_network((address, 64), strict=False)}"
    return f"ip:{address}"
