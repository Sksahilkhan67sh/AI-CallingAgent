"""CP11 -- the production (and staging) configuration validator fails closed.

Complements test_cp10_config.py (engine / Dograh credentials / trigger mode / enums).
Every rule here: the process REFUSES TO BOOT; nothing is defaulted or downgraded.
Error text must name the field and never echo a secret value."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_GOOD = dict(
    environment="production",
    calling_engine="dograh",
    dograh_api_base_url="https://dograh.example.invalid",
    dograh_api_key="dg_not_a_real_key_Zq8Wm3Rt6Yh1",
    dograh_trigger_uuid="33333333-3333-3333-3333-333333333333",
    dograh_trigger_mode="production",
    jwt_signing_key="Jx7Qp2mV9rT4wZc8LdN3sH6yB1fKaE5uGo0iXvR2",
    admin_password="a-real-admin-pw-7Hq2",
    operator_password="a-real-operator-pw-9Zk4",
    telephony_webhook_secret="tw-Mn8Rb3Vc6Xy1Qs5Ld9Fg2Hj7Kp4Tz0Wa",
    dograh_webhook_secret="dw-Ue4Yi8Po2As6Df0Gh3Jk7Lz1Xc5Vb9Nm",
    admin_username="ops-admin",
    operator_username="ops-operator",
    admin_cors_origins=["https://admin.example.invalid"],
)


def _prod(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_GOOD, **overrides})


def test_a_fully_specified_production_config_boots():
    assert _prod().environment == "production"


# --- dev-default secrets: production AND staging -----------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "jwt_signing_key",
        "admin_password",
        "operator_password",
        "telephony_webhook_secret",
        "dograh_webhook_secret",
    ],
)
@pytest.mark.parametrize("environment", ["production", "staging"])
def test_dev_default_secrets_are_refused_in_production_and_staging(field, environment):
    # Settings(...) without the field falls back to the published dev-only default.
    values = {k: v for k, v in _GOOD.items() if k != field}
    values["environment"] = environment
    with pytest.raises(ValidationError, match=f"{field.upper()} is still set to its dev-only"):
        Settings(_env_file=None, **values)


def test_development_and_test_environments_still_boot_on_defaults():
    for environment in ("development", "test"):
        assert Settings(_env_file=None, environment=environment).environment == environment


def test_staging_does_not_get_the_production_only_rules():
    """Staging may use test-mode Dograh and the native engine; only default secrets are refused."""
    ok = Settings(
        _env_file=None,
        environment="staging",
        jwt_signing_key=_GOOD["jwt_signing_key"],
        admin_password=_GOOD["admin_password"],
        operator_password=_GOOD["operator_password"],
        telephony_webhook_secret=_GOOD["telephony_webhook_secret"],
        dograh_webhook_secret=_GOOD["dograh_webhook_secret"],
    )
    assert ok.calling_engine == "native"


# --- strength -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["jwt_signing_key", "telephony_webhook_secret", "dograh_webhook_secret"]
)
def test_short_machine_secrets_are_refused(field):
    with pytest.raises(ValidationError, match=f"{field.upper()} must be at least 32 characters"):
        _prod(**{field: "x7Kq9Zm2Rt4Wp6Yh8"})  # 17 chars: not the dev default, still too short


@pytest.mark.parametrize(
    "field", ["jwt_signing_key", "telephony_webhook_secret", "dograh_webhook_secret"]
)
def test_repetitive_machine_secrets_are_refused(field):
    with pytest.raises(ValidationError, match=f"{field.upper()} is too repetitive"):
        _prod(**{field: "ab" * 20})  # 40 chars, 2 distinct


@pytest.mark.parametrize("field", ["admin_password", "operator_password"])
def test_short_passwords_are_refused(field):
    with pytest.raises(ValidationError, match=f"{field.upper()} must be at least 12 characters"):
        _prod(**{field: "Sh0rt-pw1"})


@pytest.mark.parametrize(
    "field",
    ["jwt_signing_key", "telephony_webhook_secret", "dograh_webhook_secret", "admin_password"],
)
def test_secrets_with_surrounding_whitespace_are_refused(field):
    with pytest.raises(ValidationError, match="must not start or end with whitespace"):
        _prod(**{field: " " + _GOOD[field]})


def test_a_32_char_secret_and_12_char_password_are_accepted_at_the_floor():
    assert _prod(
        jwt_signing_key="Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk",
        admin_password="Aa1Bb2Cc3Dd4",
    )


# --- distinctness -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("jwt_signing_key", "telephony_webhook_secret"),
        ("jwt_signing_key", "dograh_webhook_secret"),
        ("telephony_webhook_secret", "dograh_webhook_secret"),
        ("admin_password", "operator_password"),
        ("dograh_webhook_secret", "dograh_api_key"),
    ],
)
def test_reusing_one_secret_for_two_purposes_is_refused(a, b):
    shared = "Shared-Value-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8"
    with pytest.raises(ValidationError, match="must differ from"):
        _prod(**{a: shared, b: shared})


# --- identities / CORS ---------------------------------------------------------------------------


def test_admin_and_operator_usernames_must_differ():
    with pytest.raises(ValidationError, match="USERNAME must differ"):
        _prod(admin_username="same", operator_username="same")


@pytest.mark.parametrize("name", ["admin_username", "operator_username"])
def test_blank_usernames_are_refused(name):
    with pytest.raises(ValidationError, match="USERNAME must not be empty"):
        _prod(**{name: "  "})


def test_wildcard_cors_is_refused_in_production():
    with pytest.raises(ValidationError, match="ADMIN_CORS_ORIGINS"):
        _prod(admin_cors_origins=["https://admin.example.invalid", "*"])


# --- the new CP11 limits cannot be configured into nonsense --------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "enqueue_rate_limit_per_minute",
        "import_rate_limit_per_minute",
        "mutation_rate_limit_per_minute",
        "analysis_rate_limit_per_minute",
        "max_request_body_bytes",
        "max_import_body_bytes",
        "dograh_transcript_max_bytes",
        "dograh_transcript_max_lines",
    ],
)
@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_limits_are_refused_in_every_environment(name, bad):
    with pytest.raises(ValidationError, match=f"{name.upper()} must be > 0"):
        Settings(_env_file=None, **{name: bad})


# --- no secret ever appears in the error --------------------------------------------------------


def test_validation_errors_never_echo_secret_values():
    weak = "Wk-9Pq-short"
    with pytest.raises(ValidationError) as exc:
        _prod(jwt_signing_key=weak, telephony_webhook_secret=weak, admin_password=weak)
    text = str(exc.value)
    assert weak not in text
    assert "JWT_SIGNING_KEY" in text


def test_all_problems_are_reported_together_not_one_per_restart():
    with pytest.raises(ValidationError) as exc:
        _prod(jwt_signing_key="short", admin_password="short", admin_cors_origins=["*"])
    text = str(exc.value)
    assert "JWT_SIGNING_KEY" in text and "ADMIN_PASSWORD" in text and "ADMIN_CORS_ORIGINS" in text


def test_invalid_environment_still_cannot_downgrade_checks():
    with pytest.raises(ValidationError, match="ENVIRONMENT must be one of"):
        Settings(_env_file=None, environment="prod")  # a typo must not skip production checks


def test_startup_error_never_dumps_raw_settings_input():
    """pydantic appends `input_value={...}` -- a truncated dump of ALL settings, which
    includes REDIS_URL / PRIMARY_DB_URL credentials -- unless told not to."""
    with pytest.raises(ValidationError) as exc:
        _prod(
            jwt_signing_key="short",
            redis_url="redis://:hunter2-redis-pw@redis.internal:6379/0",
            primary_db_url="postgresql+psycopg://app:hunter2-db-pw@db.internal/app",
        )
    text = str(exc.value)
    assert "input_value" not in text
    assert "hunter2" not in text
    assert "JWT_SIGNING_KEY must be at least 32 characters" in text  # still actionable
