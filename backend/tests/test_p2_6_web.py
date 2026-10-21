"""P2-6: the API runs several processes and shuts down gracefully.

Real subprocesses through the real launcher (`python -m app.serve`) for the acceptance checks;
static checks for the container and compose wiring."""

import re
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import yaml

from app.core.config import Settings, get_settings

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
_ENV = ("WEB_CONCURRENCY", "WEB_GRACEFUL_SHUTDOWN_SECONDS", "WEB_KEEP_ALIVE_SECONDS")


@pytest.fixture
def env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# -- settings ---------------------------------------------------------------------------------


def test_web_defaults_are_one_process_with_a_bounded_shutdown(env) -> None:
    s = Settings()
    assert (s.web_concurrency, s.web_graceful_shutdown_seconds, s.web_keep_alive_seconds) == (
        1,
        30,
        5,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEB_CONCURRENCY", "0"),
        ("WEB_GRACEFUL_SHUTDOWN_SECONDS", "0"),
        ("WEB_KEEP_ALIVE_SECONDS", "0"),
    ],
)
def test_nonsense_web_settings_are_rejected(env, name: str, value: str) -> None:
    env.setenv(name, value)
    with pytest.raises(ValueError):  # noqa: PT011
        Settings()


def test_the_launcher_hands_the_settings_to_uvicorn(env) -> None:
    import uvicorn

    from app import serve

    env.setenv("WEB_CONCURRENCY", "3")
    env.setenv("WEB_GRACEFUL_SHUTDOWN_SECONDS", "12")
    env.setenv("WEB_KEEP_ALIVE_SECONDS", "7")
    seen: dict = {}
    env.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    serve.main([])
    assert seen["app"] == "app.main:app"
    assert (seen["workers"], seen["timeout_graceful_shutdown"], seen["timeout_keep_alive"]) == (
        3,
        12,
        7,
    )


# -- container and compose [READ] ------------------------------------------------------------------


def test_the_image_starts_the_api_through_the_launcher() -> None:
    cmd = re.search(r"^CMD (.+)$", (BACKEND / "Dockerfile").read_text(), flags=re.MULTILINE)
    assert cmd and "app.serve" in cmd.group(1) and "uvicorn" not in cmd.group(1)


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", value)
    assert match, f"stop_grace_period must be written in seconds, got {value!r}"
    return int(match.group(1))


def test_compose_gives_every_process_longer_to_stop_than_its_own_shutdown_takes(env) -> None:
    s = Settings()
    services = COMPOSE["services"]
    # docker stop sends SIGTERM and kills after the grace period: it must outlast the app's own.
    assert _seconds(services["backend"]["stop_grace_period"]) > s.web_graceful_shutdown_seconds
    for name in ("worker", "analysis-worker"):  # they finish the job in flight on SIGTERM
        assert _seconds(services[name]["stop_grace_period"]) > s.job_deadline_seconds, name
    assert "stop_grace_period" in services["sweeper"]


def test_the_budget_document_counts_one_process_per_web_concurrency(env) -> None:
    doc = (REPO / "docs" / "hardening-p2" / "CONNECTION-BUDGET.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| backend |"))
    processes = int(row.split("|")[3])
    assert processes == Settings().web_concurrency
    assert "WEB_CONCURRENCY" in doc


# -- real processes ------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, **env: str) -> subprocess.Popen[str]:
    import os

    full = {k: v for k, v in os.environ.items() if k not in _ENV}
    full.update(env)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "app.serve", "--app", "tests.p26_app:app", "--port", str(port),
         "--host", "127.0.0.1"],
        cwd=BACKEND,
        env=full,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/pid", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise AssertionError("server did not start: " + (proc.stdout.read() if proc.stdout else ""))


def _fresh_connection_get(url: str) -> dict:
    with httpx.Client(timeout=30) as client:  # a new connection every call
        return client.get(url).json()


def test_with_two_web_processes_requests_are_served_by_two_pids() -> None:
    port = _free_port()
    proc = _serve(port, WEB_CONCURRENCY="2")
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            pids = {
                r["pid"]
                for r in pool.map(
                    _fresh_connection_get, [f"http://127.0.0.1:{port}/pid"] * 96
                )
            }
        assert len(pids) == 2, pids
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)


def test_sigterm_lets_in_flight_requests_finish_then_exits() -> None:
    port = _free_port()
    proc = _serve(port, WEB_CONCURRENCY="2", WEB_GRACEFUL_SHUTDOWN_SECONDS="20")
    try:
        url = f"http://127.0.0.1:{port}/slow?seconds=3"
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_fresh_connection_get, url) for _ in range(4)]
            time.sleep(1)  # all four are inside the handler
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            results = [f.result(timeout=30) for f in futures]
        assert results == [{"slept": 3.0}] * 4  # none was cut off
        assert proc.wait(timeout=30) == 0
        assert time.monotonic() - started < 20
        with pytest.raises(httpx.HTTPError):  # and it no longer accepts connections
            httpx.get(f"http://127.0.0.1:{port}/pid", timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()


def _status_or_error(url: str) -> str:
    try:
        with httpx.Client(timeout=30) as client:
            return str(client.get(url).status_code)
    except httpx.HTTPError as exc:
        return type(exc).__name__


def test_a_request_that_outlasts_the_grace_period_cannot_hang_the_shutdown() -> None:
    port = _free_port()
    proc = _serve(port, WEB_CONCURRENCY="1", WEB_GRACEFUL_SHUTDOWN_SECONDS="2")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(_status_or_error, f"http://127.0.0.1:{port}/slow?seconds=30")
            time.sleep(1)
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
            assert time.monotonic() - started < 15  # grace 2 s, not the request's 30 s
            assert slow.result(timeout=30) != "200"  # it was cut off, not silently completed
    finally:
        if proc.poll() is None:
            proc.kill()


# -- the background processes: SIGTERM is a prompt, clean exit (no hang) --------------------------


@pytest.mark.parametrize(
    ("module", "role"),
    [("app.worker", "worker"), ("app.analysis_worker", "analysis"), ("app.sweeper", "sweeper")],
)
def test_background_processes_exit_cleanly_and_promptly_on_sigterm(module: str, role: str) -> None:
    import os

    env = {k: v for k, v in os.environ.items()}
    env["DB_ROLE"] = role
    proc = subprocess.Popen(  # noqa: S603  (the same command compose uses)
        [sys.executable, "-m", module],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(6)  # past start-up: it is now idling in its loop
        assert proc.poll() is None, "the process died by itself: " + (proc.stdout.read() or "")
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        assert time.monotonic() - started < 15
    finally:
        if proc.poll() is None:
            proc.kill()
