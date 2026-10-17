"""P1-3 -- Redis holds the dial queue, the retry cache and live counters, so production must not
run it open, and the compose stack must not run it without persistence.

Two things are checked without Docker:
  * the production config validator refuses a Redis URL that carries no password;
  * docker-compose.yml's redis service has AOF (everysec), requirepass, a healthcheck, a named
    volume and no published port, and every service's REDIS_URL carries the password.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tests.test_cp11_production_config import _prod

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
GOOD_URL = "redis://:a-real-redis-pw-4Tz9@redis.example.invalid:6379/0"


# -- production validator ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost:6379/0",
        "redis://redis:6379",
        "redis://redis.example.invalid:6379/0",
        "redis://app-user@redis.example.invalid:6379/0",  # a username is not a password
        "redis://:@redis.example.invalid:6379/0",  # empty password
        "rediss://redis.example.invalid:6380/0",  # TLS does not replace auth
    ],
)
def test_production_refuses_a_redis_url_without_a_password(url):
    with pytest.raises(ValidationError) as exc:
        _prod(redis_url=url)
    message = str(exc.value)
    assert "REDIS_URL" in message and "password" in message
    assert url not in message  # the URL is never echoed


@pytest.mark.parametrize(
    "url",
    [
        GOOD_URL,
        "redis://default:a-real-redis-pw-4Tz9@redis.example.invalid:6379/1",
        "rediss://:a-real-redis-pw-4Tz9@redis.example.invalid:6380/0",
    ],
)
def test_production_accepts_a_redis_url_with_a_password(url):
    assert _prod(redis_url=url).redis_url == url


def test_the_error_never_echoes_the_password():
    with pytest.raises(ValidationError) as exc:
        _prod(redis_url="redis://:topsecretpw123@redis.example.invalid:6379/0", calling_engine="x")
    assert "topsecretpw123" not in str(exc.value)


def test_development_staging_and_test_do_not_require_redis_auth():
    from app.core.config import Settings

    for environment in ("development", "test"):
        assert Settings(_env_file=None, environment=environment).environment == environment
    assert _prod(environment="staging", redis_url="redis://redis:6379/0")


# -- compose -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def _redis_command(compose) -> str:
    command = compose["services"]["redis"].get("command", "")
    return " ".join(command) if isinstance(command, list) else str(command)


def test_compose_redis_persists_with_aof_everysec(compose):
    command = _redis_command(compose)
    assert "--appendonly yes" in command
    assert "--appendfsync everysec" in command
    mounts = compose["services"]["redis"].get("volumes", [])
    assert any(str(m).split(":")[0] == "redis_data" and str(m).endswith(":/data") for m in mounts)
    assert "redis_data" in compose["volumes"]


def test_compose_redis_requires_a_password_and_has_an_authenticated_healthcheck(compose):
    redis = compose["services"]["redis"]
    assert "--requirepass" in _redis_command(compose)
    assert "REDIS_PASSWORD" in str(redis.get("environment", "")) + _redis_command(compose)
    assert "healthcheck" in redis
    check = " ".join(redis["healthcheck"]["test"])
    authenticated = "REDISCLI_AUTH" in str(redis.get("environment", "")) or "-a" in check
    assert "ping" in check and authenticated


def test_compose_does_not_publish_the_redis_port(compose):
    assert "ports" not in compose["services"]["redis"]


def test_every_service_that_uses_redis_passes_the_password(compose):
    users = [
        name
        for name, svc in compose["services"].items()
        if any(str(e).startswith("REDIS_URL=") for e in svc.get("environment", []))
    ]
    assert set(users) >= {"backend", "worker", "analysis-worker"}
    for name in users:
        env = compose["services"][name]["environment"]
        url = next(str(e) for e in env if str(e).startswith("REDIS_URL="))
        assert ":${REDIS_PASSWORD" in url, f"{name} connects to Redis without the password"


def test_the_restart_drill_script_is_valid_bash_and_executable():
    import os
    import subprocess

    script = COMPOSE.parent / "ops" / "redis" / "restart_drill.sh"
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
