"""Provider selection -- Checkpoint 03 Step 14: provider choice comes
from configuration, not a hardcoded branch scattered through the app.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.services.telephony.base import TelephonyProvider
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
