"""Mock telephony provider -- Checkpoint 03 Step 13.

No real provider is named in the reconciled spec and no provider
credentials exist in this environment, so this is the only concrete
`TelephonyProvider` implementation for now (see
docs/CHECKPOINT-03-NOTES.md). It never claims a real call was placed --
`provider_call_id`s are synthetic, generated locally.

Tests configure behavior by phone number via `set_outcome`, so the same
adapter can exercise success, failure, and ambiguous-outcome paths
without any network access.
"""

import uuid
from collections.abc import Callable

from app.models.enums import NeverConnectedFailureReason
from app.services.telephony.base import (
    ProviderCallResult,
    ProviderOutcome,
    TelephonyProvider,
)


class MockTelephonyProvider(TelephonyProvider):
    name = "mock"

    def __init__(self) -> None:
        # provider_call_id -> ProviderCallResult, so get_call_status can
        # answer a reconciliation query the way a real provider's status
        # API would.
        self._calls: dict[str, ProviderCallResult] = {}
        # idempotency_key -> provider_call_id, for find_call_by_idempotency_key.
        self._by_idempotency_key: dict[str, str] = {}
        # to_number -> a function producing this call's outcome, for
        # tests. Defaults to "always succeeds."
        self._outcome_overrides: dict[str, Callable[[], ProviderCallResult]] = {}

    def set_outcome(self, to_number: str, outcome_fn: Callable[[], ProviderCallResult]) -> None:
        self._outcome_overrides[to_number] = outcome_fn

    def simulate_provider_side_call_exists(
        self, idempotency_key: str, provider_call_id: str
    ) -> None:
        """Test helper for Step 36: simulates "the create-call request
        timed out from our side, but the provider actually did create
        the call" -- i.e. what find_call_by_idempotency_key should find
        during reconciliation, independent of what create_outbound_call
        itself returned."""
        result = ProviderCallResult(
            outcome=ProviderOutcome.INITIATED, provider_call_id=provider_call_id
        )
        self._calls[provider_call_id] = result
        self._by_idempotency_key[idempotency_key] = provider_call_id

    def create_outbound_call(
        self, *, to_number: str, idempotency_key: str
    ) -> ProviderCallResult:
        override = self._outcome_overrides.get(to_number)
        result = override() if override else self._default_success()

        if result.outcome == ProviderOutcome.INITIATED and result.provider_call_id:
            self._calls[result.provider_call_id] = result
            self._by_idempotency_key[idempotency_key] = result.provider_call_id

        return result

    def get_call_status(self, provider_call_id: str) -> ProviderCallResult:
        result = self._calls.get(provider_call_id)
        if result is None:
            return ProviderCallResult(outcome=ProviderOutcome.FAILED)
        return result

    def find_call_by_idempotency_key(self, idempotency_key: str) -> ProviderCallResult:
        provider_call_id = self._by_idempotency_key.get(idempotency_key)
        if provider_call_id is None:
            return ProviderCallResult(outcome=ProviderOutcome.FAILED)
        return self.get_call_status(provider_call_id)

    def _default_success(self) -> ProviderCallResult:
        return ProviderCallResult(
            outcome=ProviderOutcome.INITIATED, provider_call_id=f"mock-{uuid.uuid4().hex[:12]}"
        )


def always_fails(reason: NeverConnectedFailureReason) -> Callable[[], ProviderCallResult]:
    """Test helper: `provider.set_outcome(number, always_fails(...))`."""
    return lambda: ProviderCallResult(outcome=ProviderOutcome.FAILED, failure_reason=reason)


def always_ambiguous() -> ProviderCallResult:
    return ProviderCallResult(outcome=ProviderOutcome.AMBIGUOUS)
