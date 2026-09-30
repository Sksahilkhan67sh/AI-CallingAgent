"""Provider selection -- Checkpoint 03 Step 14: provider choice comes
from configuration, not a hardcoded branch scattered through the app.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.services.telephony.base import TelephonyProvider
from app.services.telephony.dograh_client import DograhClient
from app.services.telephony.mock_provider import MockTelephonyProvider


@lru_cache
def get_telephony_provider() -> TelephonyProvider:
    settings = get_settings()
    if settings.telephony_provider == "mock":
        return MockTelephonyProvider()
    raise ValueError(
        f"Unsupported TELEPHONY_PROVIDER '{settings.telephony_provider}' -- only "
        "'mock' is implemented (see docs/CHECKPOINT-03-NOTES.md)"
    )


@lru_cache
def get_dograh_client() -> DograhClient:
    """Checkpoint 08: only constructed when calling_engine="dograh" --
    callers must not call this under the native/mock engine, since it
    raises DograhConfigurationError if the required settings aren't
    present."""
    settings = get_settings()
    return DograhClient(
        base_url=settings.dograh_api_base_url,
        api_key=settings.dograh_api_key,
        trigger_uuid=settings.dograh_trigger_uuid,
        mode=settings.dograh_trigger_mode,
        timeout=settings.dograh_request_timeout_seconds,
        connect_timeout=settings.dograh_connect_timeout_seconds,
        workflow_id=settings.dograh_workflow_id,
    )
