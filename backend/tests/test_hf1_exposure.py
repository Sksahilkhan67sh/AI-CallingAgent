"""HF1 1.4 -- production exposure.

In staging and production the interactive API docs and the OpenAPI schema (a free map of
every route and payload) are not served; /health returns no environment name, and /ready
never carries exception text, host names or secrets when a dependency is down.
"""

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.admin import ComponentHealth
from tests.test_cp11_production_config import _GOOD

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def _settings(environment: str) -> Settings:
    if environment in ("production", "staging"):
        return Settings(_env_file=None, **{**_GOOD, "environment": environment})
    return Settings(_env_file=None, environment=environment)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_docs_and_schema_are_not_served_in_staging_and_production(environment):
    http = TestClient(create_app(_settings(environment)))
    for path in _DOC_PATHS:
        assert http.get(path).status_code == 404, path


@pytest.mark.parametrize("environment", ["development", "test"])
def test_docs_and_schema_stay_available_in_development_and_test(environment):
    http = TestClient(create_app(_settings(environment)))
    assert http.get("/docs").status_code == 200
    assert http.get("/redoc").status_code == 200
    schema = http.get("/openapi.json")
    assert schema.status_code == 200 and "/health" in schema.json()["paths"]


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_health_never_reports_the_environment(environment):
    http = TestClient(create_app(_settings(environment)))
    response = http.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "environment" not in body
    assert environment not in response.text
    assert set(body) <= {"status", "service"}


def test_the_real_app_object_is_built_from_the_same_factory():
    from app.main import app

    assert app.docs_url in ("/docs", None)  # development in the test environment


# --- /ready never leaks ----------------------------------------------------------------

_LEAKY = "connection to server at db.internal.example (10.9.8.7) failed: password=hunter2"


def test_ready_hides_exception_text_hosts_and_secrets_when_postgres_is_down(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.health.check_postgres",
        lambda db: ComponentHealth(name="PostgreSQL", status="degraded", detail=_LEAKY),
    )
    response = client.get("/ready")
    assert response.status_code == 503
    for needle in ("db.internal", "10.9.8.7", "hunter2", "password", "connection to server"):
        assert needle not in response.text
    assert {c["name"]: c["status"] for c in response.json()["components"]}["PostgreSQL"] == (
        "degraded"
    )


def test_ready_hides_exception_text_when_redis_is_really_unreachable(client, monkeypatch):
    dead = redis_lib.Redis.from_url(
        "redis://:topsecretpw@redis.internal.example:1/0",
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )
    monkeypatch.setattr("app.services.admin.system_service.get_redis", lambda: dead)
    response = client.get("/ready")
    assert response.status_code == 503
    for needle in ("topsecretpw", "redis.internal", "Error", "refused", "Name or service"):
        assert needle not in response.text
    body = response.json()
    assert set(body) == {"status", "components"}
    assert all(set(c) == {"name", "status"} for c in body["components"])


def test_ready_has_no_environment_either(client):
    assert "environment" not in client.get("/ready").text
