"""P2-4: threadpool vs pool sizing and the connection budget.

Pure configuration and static-file checks plus one in-process app start; no database, no clock."""

import re
from pathlib import Path

import anyio
import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app

REPO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
BUDGET_DOC = (REPO / "docs" / "hardening-p2" / "CONNECTION-BUDGET.md").read_text()
_ENV = ("API_THREADPOOL_SIZE", "DB_MAX_CONNECTIONS_BUDGET", "DB_POOL_SIZE", "DB_MAX_OVERFLOW")


@pytest.fixture
def env(monkeypatch):
    for name in _ENV + ("PRIMARY_DB_POOL_SIZE",):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# -- settings and the validator -------------------------------------------------------------


def test_the_threadpool_defaults_to_eight_times_the_pool_capacity(env) -> None:
    # Measured in P2-8: threads equal to (or twice) the connections stall the pool under load.
    s = Settings()
    assert s.api_threadpool_size is None
    assert s.pool_capacity == 15
    assert s.effective_api_threadpool_size == 120
    env.setenv("DB_POOL_SIZE", "4")
    env.setenv("DB_MAX_OVERFLOW", "2")
    assert Settings().effective_api_threadpool_size == 48  # follows the pool, not a fixed number


def test_an_explicit_threadpool_size_wins(env) -> None:
    env.setenv("API_THREADPOOL_SIZE", "40")
    assert Settings().effective_api_threadpool_size == 40


def test_a_zero_threadpool_is_rejected(env) -> None:
    env.setenv("API_THREADPOOL_SIZE", "0")
    with pytest.raises(ValueError):  # noqa: PT011
        Settings()


def test_a_pool_larger_than_the_stated_budget_stops_the_boot(env) -> None:
    env.setenv("DB_MAX_CONNECTIONS_BUDGET", "10")  # capacity is 5 + 10 = 15
    with pytest.raises(ValueError, match="DB_MAX_CONNECTIONS_BUDGET"):
        Settings()


@pytest.mark.parametrize("budget", ["15", "20"])
def test_a_pool_within_the_budget_boots(env, budget: str) -> None:
    env.setenv("DB_MAX_CONNECTIONS_BUDGET", budget)
    assert Settings().db_max_connections_budget == int(budget)


def test_a_threadpool_below_twice_the_pool_capacity_is_warned_about(env) -> None:
    from app.core.database import threadpool_warnings

    assert threadpool_warnings(Settings()) == []  # the default is 8x
    env.setenv("API_THREADPOOL_SIZE", "30")  # exactly 2 x 15
    assert threadpool_warnings(Settings()) == []
    env.setenv("API_THREADPOOL_SIZE", "15")  # one thread per connection: the measured stall
    (message,) = threadpool_warnings(Settings())
    assert "API_THREADPOOL_SIZE" in message and "DB_POOL_TIMEOUT" in message


# -- the limiter really is applied when the app starts -------------------------------------------


def _limiter_seen_by_the_app(settings: Settings) -> int:
    application = create_app(settings)

    @application.get("/_limiter")
    async def limiter() -> dict[str, float]:
        return {"tokens": anyio.to_thread.current_default_thread_limiter().total_tokens}

    with TestClient(application) as client:  # `with` runs the lifespan
        return int(client.get("/_limiter").json()["tokens"])


def test_startup_sets_the_anyio_threadpool_to_the_effective_size(env) -> None:
    assert _limiter_seen_by_the_app(Settings()) == 120
    env.setenv("API_THREADPOOL_SIZE", "7")
    assert _limiter_seen_by_the_app(Settings()) == 7


# -- the budget document matches the real compose file [READ] ---------------------------------


def _doc_table(heading: str) -> list[list[str]]:
    section = BUDGET_DOC.split(heading, 1)[1].split("\n## ", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.strip().startswith("|")
    ]
    return rows[2:]  # header and separator


def _compose_db_services() -> dict[str, dict]:
    return {
        name: svc
        for name, svc in COMPOSE["services"].items()
        if any(str(e).startswith("PRIMARY_DB_URL=") for e in svc.get("environment", []))
        and name != "migrate"  # one-shot, counted in the budget's Reserve table
    }


def test_the_budget_table_lists_exactly_the_compose_services_that_use_the_database() -> None:
    rows = {r[0]: r for r in _doc_table("## Compose defaults")}
    assert set(rows) == set(_compose_db_services())


def test_each_budget_row_matches_the_default_pool_settings() -> None:
    s = Settings()
    for service, replicas, processes, pool, overflow, total in _doc_table("## Compose defaults"):
        assert (int(pool), int(overflow)) == (s.db_pool_size, s.db_max_overflow), service
        assert int(total) == int(replicas) * int(processes) * (int(pool) + int(overflow)), service


def test_postgres_max_connections_is_explicit_and_covers_the_budget() -> None:
    command = COMPOSE["services"]["db"].get("command", [])
    match = re.search(r"max_connections=(\d+)", " ".join(map(str, command)))
    assert match, "docker-compose.yml must set max_connections explicitly"
    max_connections = int(match.group(1))
    total = sum(int(r[5]) for r in _doc_table("## Compose defaults"))
    reserve = sum(int(r[1]) for r in _doc_table("## Reserve"))
    assert total + reserve <= max_connections
    assert f"max_connections={max_connections}" in BUDGET_DOC


def test_the_new_settings_are_documented() -> None:
    for name in ("API_THREADPOOL_SIZE", "DB_MAX_CONNECTIONS_BUDGET"):
        for path in ("backend/.env.example", "docs/hardening-p2/SETTINGS.md"):
            assert name in (REPO / path).read_text(), f"{name} missing from {path}"
