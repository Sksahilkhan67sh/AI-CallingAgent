"""CP15A -- retention / erasure settings and their production validation."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests.cp15_helpers import CP15_PRODUCTION_SETTINGS
from tests.test_cp11_production_config import _GOOD

_PERIODS = dict(CP15_PRODUCTION_SETTINGS)
_BASE = {k: v for k, v in _GOOD.items() if k not in _PERIODS}  # production minus CP15 settings


def _prod(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **_PERIODS, **overrides})


def test_a_fully_specified_production_config_boots():
    assert _prod().retention_transcript_days == 180


@pytest.mark.parametrize("name", [k for k in _PERIODS if k.startswith("retention_")])
def test_unset_retention_is_a_production_startup_error(name):
    values = {k: v for k, v in _PERIODS.items() if k != name}
    with pytest.raises(ValidationError, match=f"{name.upper()} must be set explicitly"):
        Settings(_env_file=None, **{**_BASE, **values})


def test_zero_means_keep_forever_and_is_accepted_in_production():
    s = _prod(**{k: 0 for k in _PERIODS if k.startswith("retention_")})
    assert s.retention_audit_log_days == 0


def test_unset_retention_in_development_means_keep_forever():
    s = Settings(_env_file=None, environment="development")
    assert s.retention_transcript_days is None


def test_negative_retention_is_refused_everywhere():
    with pytest.raises(ValidationError, match="RETENTION_ANALYSIS_DAYS must be >= 0"):
        Settings(_env_file=None, environment="development", retention_analysis_days=-1)


def test_processed_event_retention_below_the_provider_retry_horizon_is_refused():
    with pytest.raises(ValidationError, match="RETENTION_PROCESSED_EVENT_DAYS must be at least"):
        _prod(retention_processed_event_days=1)


@pytest.mark.parametrize("pepper", ["", "short", "a" * 40])
def test_a_missing_or_weak_pepper_is_refused_in_production(pepper):
    with pytest.raises(ValidationError, match="ERASURE_FINGERPRINT_PEPPER"):
        _prod(erasure_fingerprint_pepper=pepper)


def test_the_pepper_must_differ_from_the_other_secrets():
    with pytest.raises(ValidationError, match="ERASURE_FINGERPRINT_PEPPER must differ"):
        _prod(erasure_fingerprint_pepper=_GOOD["jwt_signing_key"])


def test_batch_and_runtime_bounds():
    with pytest.raises(ValidationError, match="RETENTION_BATCH_SIZE"):
        Settings(_env_file=None, retention_batch_size=0)
    with pytest.raises(ValidationError, match="RETENTION_MAX_RUNTIME_SECONDS"):
        Settings(_env_file=None, retention_max_runtime_seconds=0)


def test_validation_errors_never_echo_the_pepper():
    with pytest.raises(ValidationError) as err:
        _prod(erasure_fingerprint_pepper="x" * 5)
    assert "xxxxx" not in str(err.value)
