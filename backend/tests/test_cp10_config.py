"""Checkpoint 10 -- provider/voice configuration is environment-driven and
fails closed: production can never silently run the mock/native engine, and a
typo in a closed-set setting cannot downgrade safety checks."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

_PROD_SECRETS = dict(
    jwt_signing_key="k" * 32,
    admin_password="a-real-admin-pw",
    operator_password="a-real-operator-pw",
    telephony_webhook_secret="s-telephony",
    dograh_webhook_secret="s-dograh",
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _production(**overrides) -> Settings:
    base = dict(
        environment="production",
        calling_engine="dograh",
        dograh_api_base_url="https://dograh.example.invalid",
        dograh_api_key="dg_not_a_real_key",
        dograh_trigger_uuid="33333333-3333-3333-3333-333333333333",
        dograh_trigger_mode="production",
        **_PROD_SECRETS,
    )
    base.update(overrides)
    return _settings(**base)


def test_development_defaults_boot_without_any_provider_credentials():
    s = _settings()
    assert (s.environment, s.calling_engine) == ("development", "native")


def test_complete_production_dograh_config_is_accepted():
    assert _production().calling_engine == "dograh"


def test_production_refuses_the_native_mock_engine():
    with pytest.raises(ValidationError, match="CALLING_ENGINE must be 'dograh'"):
        _production(calling_engine="native")


@pytest.mark.parametrize("missing", ["dograh_api_key", "dograh_trigger_uuid"])
def test_production_requires_dograh_credentials(missing):
    with pytest.raises(ValidationError, match=missing.upper()):
        _production(**{missing: ""})


def test_production_refuses_dograh_test_mode():
    with pytest.raises(ValidationError, match="DOGRAH_TRIGGER_MODE"):
        _production(dograh_trigger_mode="test")


@pytest.mark.parametrize(
    "url",
    [
        "http://dograh.example.invalid",  # API key would travel in clear text
        "https://localhost:8000",
        "https://127.0.0.1",
        "ftp://dograh.example.invalid",
        "dograh.example.invalid",  # no scheme
    ],
)
def test_production_requires_https_non_local_dograh_url(url):
    with pytest.raises(ValidationError, match="DOGRAH_API_BASE_URL"):
        _production(dograh_api_base_url=url)


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "prod"),  # a typo here would skip every production check
        ("environment", "Production"),
        ("calling_engine", "Dograh"),  # a typo here would silently run the native engine
        ("calling_engine", ""),
        ("dograh_trigger_mode", "prod"),  # would silently hit the /test/ endpoint
    ],
)
def test_closed_set_settings_reject_typos_in_every_environment(field, value):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


@pytest.mark.parametrize("field", ["dograh_connect_timeout_seconds", "dograh_read_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_timeouts_are_rejected(field, value):
    with pytest.raises(ValidationError, match="TIMEOUT"):
        _settings(**{field: value})


def test_staging_dograh_requires_credentials_but_may_use_test_mode():
    with pytest.raises(ValidationError, match="DOGRAH_API_KEY"):
        _settings(environment="staging", calling_engine="dograh", dograh_trigger_uuid="u")
    ok = _settings(
        environment="staging",
        calling_engine="dograh",
        dograh_api_key="k",
        dograh_trigger_uuid="u",
        dograh_trigger_mode="test",
    )
    assert ok.dograh_trigger_mode == "test"


def test_readiness_reports_missing_dograh_credentials(client, monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    monkeypatch.setenv("DOGRAH_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.get("/ready")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503
    components = {c["name"]: c["status"] for c in response.json()["components"]}
    assert components["Dograh configuration"] == "degraded"


def test_readiness_ok_with_dograh_credentials_and_hides_them(client, monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    monkeypatch.setenv("DOGRAH_API_KEY", "dg_secret_value")
    monkeypatch.setenv("DOGRAH_TRIGGER_UUID", "44444444-4444-4444-4444-444444444444")
    get_settings.cache_clear()
    try:
        response = client.get("/ready")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    assert "dg_secret_value" not in response.text
    assert "44444444-4444" not in response.text
