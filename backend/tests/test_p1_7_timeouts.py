"""P1-7 -- every outbound call has an explicit connect and read timeout, from settings.

Inventory of outbound I/O in app/ (all of it, found by AST, not by memory):
  * Dograh trigger, run lookup, reconciliation listing -> DograhClient._send (settings:
    DOGRAH_CONNECT_TIMEOUT_SECONDS / DOGRAH_READ_TIMEOUT_SECONDS);
  * Dograh transcript download -> transcript_fetch._download (DOGRAH_TRANSCRIPT_CONNECT/READ_*);
  * Redis -> core.redis_client.get_redis, the ONLY place a Redis client is built
    (REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS / REDIS_SOCKET_TIMEOUT_SECONDS).
There is no outbound webhook, recording download, raw socket or `requests` use. Database
statement/lock timeouts are Phase 2 and deliberately not touched here.

The guard below fails the build when a NEW httpx or redis client is created without explicit
timeouts, or when a timeout is a hard-coded number instead of coming from settings.
"""

import ast
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import redis
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services.queue.deadline import JobDeadlineExceeded, job_deadline
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhClient,
    DograhErrorCategory,
)

APP = Path(__file__).resolve().parents[1] / "app"
_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
_REDIS_CTORS = {
    "redis.Redis",
    "redis.StrictRedis",
    "redis.Redis.from_url",
    "redis.StrictRedis.from_url",
    "redis.from_url",
    "redis.ConnectionPool",
    "redis.ConnectionPool.from_url",
    "redis.asyncio.Redis",
    "redis.asyncio.Redis.from_url",
    "redis.asyncio.from_url",
    "Redis",
    "Redis.from_url",
    "ConnectionPool",
    "ConnectionPool.from_url",
}
_TIMEOUT_REQUIRED = {
    "urllib.request.urlopen",
    "socket.create_connection",
    "http.client.HTTPConnection",
    "http.client.HTTPSConnection",
    "smtplib.SMTP",
    "smtplib.SMTP_SSL",
}
_FORBIDDEN_IMPORTS = {"requests", "aiohttp", "urllib3"}


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    value_refs = False
    sends_with_timeout = False

    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_IMPORTS:
                    problems.append(f"{where} unreviewed HTTP library {alias.name!r}: add it here")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _FORBIDDEN_IMPORTS:
                problems.append(f"{where} unreviewed HTTP library {node.module!r}: add it here")
        elif isinstance(node, ast.Attribute) and id(node) not in call_funcs:
            name = _dotted(node)
            if name and name.startswith("httpx.") and name.split(".")[-1] in _VERBS:
                value_refs = True  # e.g. httpx.post passed to a helper
        elif isinstance(node, ast.Call):
            name = _dotted(node.func) or ""
            if name == "send":
                kw = _kw(node, "timeout")
                sends_with_timeout |= kw is not None and not _is_none(kw.value)
            is_httpx = name in {"httpx.Client", "httpx.AsyncClient"} or (
                name.startswith("httpx.") and name.split(".")[-1] in _VERBS
            )
            if is_httpx:
                kw = _kw(node, "timeout")
                if kw is None or _is_none(kw.value):
                    problems.append(f"{where} {name}(...) has no explicit timeout=")
            elif name in _REDIS_CTORS:
                for needed in ("socket_timeout", "socket_connect_timeout"):
                    kw = _kw(node, needed)
                    if kw is None or _is_none(kw.value):
                        problems.append(f"{where} {name}(...) has no explicit {needed}=")
            elif name in _TIMEOUT_REQUIRED:
                kw = _kw(node, "timeout")
                if kw is None or _is_none(kw.value):
                    problems.append(f"{where} {name}(...) has no explicit timeout=")
            elif name == "httpx.Timeout":
                values = [*node.args, *(k.value for k in node.keywords)]
                if values and all(
                    isinstance(v, ast.Constant) and isinstance(v.value, int | float) for v in values
                ):
                    problems.append(
                        f"{where} httpx.Timeout uses hard-coded numbers: take them from Settings"
                    )
    if value_refs and not sends_with_timeout:
        problems.append(
            f"{filename}: an httpx verb is passed as a value but no `send(..., timeout=...)` "
            "call enforces a timeout"
        )
    return problems


def test_the_app_has_no_outbound_call_without_an_explicit_timeout_from_settings():
    problems: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        problems += find_violations(path.read_text(), str(path.relative_to(APP.parent)))
    assert problems == [], "\n".join(problems)


# -- the guard itself ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nhttpx.get('http://x')\n",
        "import httpx\nhttpx.post('http://x', timeout=None)\n",
        "import httpx\nc = httpx.Client()\n",
        "import httpx\nc = httpx.AsyncClient(follow_redirects=True)\n",
        "import redis\nr = redis.Redis.from_url('redis://x')\n",
        "import redis\nr = redis.Redis(host='x', socket_timeout=3)\n",
        "import redis\nr = redis.from_url('redis://x', socket_connect_timeout=2)\n",
        "import redis\np = redis.ConnectionPool.from_url('redis://x')\n",
        "import socket\ns = socket.create_connection(('x', 1))\n",
        "import urllib.request\nurllib.request.urlopen('http://x')\n",
        "import requests\n",
        "from aiohttp import ClientSession\n",
        "import httpx\nT = httpx.Timeout(connect=5.0, read=10.0)\n",
        "import httpx\ndef f(send):\n    return send(httpx.post)\n",
    ],
)
def test_the_guard_flags_a_missing_or_hardcoded_timeout(source):
    assert find_violations(source), f"not flagged: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nhttpx.get('http://x', timeout=t)\n",
        "import httpx\nc = httpx.Client(timeout=settings_timeout())\n",
        "import httpx\nT = httpx.Timeout(connect=a, read=b, write=a, pool=a)\n",
        "import redis\nr = redis.Redis.from_url(u, socket_timeout=a, socket_connect_timeout=b)\n",
        "import socket\ns = socket.create_connection(('x', 1), timeout=3)\n",
        (
            "import httpx\n"
            "class C:\n"
            "    def _send(self, send, url):\n"
            "        return send(url, timeout=self.timeout)\n"
            "    def f(self):\n"
            "        return self._send(httpx.post, 'u')\n"
        ),
    ],
)
def test_the_guard_accepts_a_call_that_takes_its_timeout_explicitly(source):
    assert find_violations(source) == []


# -- timeouts really fire against a stalled server ---------------------------------------------


class _Stalled:
    """A TCP server that accepts connections and never answers."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self._stop = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
                self.conns.append(conn)  # held open, never read from, never answered
            except OSError:
                continue

    def close(self):
        self._stop = True
        for c in self.conns:
            c.close()
        self.sock.close()


@pytest.fixture
def stalled():
    server = _Stalled()
    yield server
    server.close()


def _dograh(port: int, connect: float = 0.5, read: float = 0.5) -> DograhClient:
    return DograhClient(
        base_url=f"http://127.0.0.1:{port}",
        api_key="k",
        trigger_uuid="u",
        mode="test",
        connect_timeout=connect,
        read_timeout=read,
    )


@pytest.mark.parametrize("call", ["trigger", "get_run", "find_runs"])
def test_every_dograh_request_gives_up_at_the_read_timeout(stalled, call):
    from datetime import UTC, datetime

    client = _dograh(stalled.port)
    started = time.monotonic()
    with pytest.raises(DograhApiError) as exc:
        if call == "trigger":
            client.trigger_call(phone_number="+919800000001", initial_context={})
        elif call == "get_run":
            client.get_run(workflow_id=1, run_id=2)
        else:
            client.find_runs_for_attempt("a", datetime.now(UTC))
    assert time.monotonic() - started < 4
    assert exc.value.category in (
        DograhErrorCategory.TIMEOUT,
        DograhErrorCategory.AMBIGUOUS_REQUEST,
    )


def test_the_dograh_factory_takes_both_timeouts_from_settings(monkeypatch):
    from app.services.telephony.factory import get_dograh_client

    settings = get_settings()
    monkeypatch.setattr(settings, "dograh_api_key", "k")
    monkeypatch.setattr(settings, "dograh_trigger_uuid", "u")
    monkeypatch.setattr(settings, "dograh_connect_timeout_seconds", 1.25)
    monkeypatch.setattr(settings, "dograh_read_timeout_seconds", 2.5)
    timeout = get_dograh_client().timeout
    assert (timeout.connect, timeout.read) == (1.25, 2.5)


def test_the_transcript_download_gives_up_at_its_read_timeout(stalled, monkeypatch):
    from app.services.telephony import transcript_fetch as tf

    settings = get_settings()
    monkeypatch.setattr(settings, "dograh_transcript_connect_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "dograh_transcript_read_timeout_seconds", 0.5)
    target = tf._Target("http", "127.0.0.1", stalled.port, "127.0.0.1", "/t.json")
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        tf._download(target)
    assert time.monotonic() - started < 4


def test_redis_gives_up_at_its_socket_timeout(stalled, monkeypatch):
    from app.core import redis_client

    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", f"redis://127.0.0.1:{stalled.port}/0")
    monkeypatch.setattr(settings, "redis_socket_connect_timeout_seconds", 0.5)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", 0.5)
    redis_client.get_redis.cache_clear()
    started = time.monotonic()
    try:
        with pytest.raises(redis.TimeoutError):
            with job_deadline(8):  # a client without a timeout would hang here for ever
                redis_client.get_redis().ping()
    except JobDeadlineExceeded:
        pytest.fail("Redis call hung: no socket timeout")
    finally:
        redis_client.get_redis.cache_clear()
    assert time.monotonic() - started < 4


def test_redis_connect_timeout_is_applied_to_the_connection_pool(monkeypatch):
    from app.core import redis_client

    settings = get_settings()
    monkeypatch.setattr(settings, "redis_socket_connect_timeout_seconds", 1.5)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", 7.5)
    redis_client.get_redis.cache_clear()
    try:
        kwargs = redis_client.get_redis().connection_pool.connection_kwargs
    finally:
        redis_client.get_redis.cache_clear()
    assert kwargs["socket_connect_timeout"] == 1.5
    assert kwargs["socket_timeout"] == 7.5


# -- settings validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "redis_socket_connect_timeout_seconds",
        "redis_socket_timeout_seconds",
        "dograh_transcript_connect_timeout_seconds",
        "dograh_transcript_read_timeout_seconds",
    ],
)
def test_timeouts_must_be_positive(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_the_redis_socket_timeout_must_outlast_the_blocking_stream_read():
    # Workers block up to 1 s on XREADGROUP; a shorter socket timeout would abort every idle poll.
    with pytest.raises(ValidationError, match="REDIS_SOCKET_TIMEOUT_SECONDS"):
        Settings(_env_file=None, redis_socket_timeout_seconds=1.0)
    assert Settings(_env_file=None).redis_socket_timeout_seconds >= 2.0
