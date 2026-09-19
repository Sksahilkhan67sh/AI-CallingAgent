"""Telephony provider abstraction -- Checkpoint 03 Steps 12-13.

The rest of the application talks to this contract only; no
`if provider == "twilio"` branching anywhere else in the codebase.
"""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models.enums import NeverConnectedFailureReason


class ProviderOutcome(str, enum.Enum):
    """Normalized result of a create-call attempt, independent of any
    one provider's response shape."""

    INITIATED = "initiated"  # provider accepted the call; a provider_call_id exists
    FAILED = "failed"  # provider confirmed the call did not/will not happen
    AMBIGUOUS = "ambiguous"  # e.g. request timed out -- unknown whether it was created


@dataclass
class ProviderCallResult:
    outcome: ProviderOutcome
    provider_call_id: str | None = None
    failure_reason: NeverConnectedFailureReason | None = None


class TelephonyProvider(ABC):
    """One conceptual operation: create_outbound_call. Everything else
    (rate limiting, concurrency, retries, circuit breaking) lives around
    this contract, not inside a specific provider's implementation."""

    name: str

    @abstractmethod
    def create_outbound_call(
        self, *, to_number: str, idempotency_key: str
    ) -> ProviderCallResult:
        """Idempotency: a provider that supports idempotency keys should
        honor `idempotency_key` so a retried request against the same
        key doesn't create two calls provider-side. Not every real
        provider supports this -- see Step 19 for how an ambiguous
        outcome (e.g. a timeout) must be reconciled by *querying*
        provider state, never by blindly retrying."""

    @abstractmethod
    def get_call_status(self, provider_call_id: str) -> ProviderCallResult:
        """Used by the webhook handler and general status checks once a
        provider_call_id is already known."""

    @abstractmethod
    def find_call_by_idempotency_key(self, idempotency_key: str) -> ProviderCallResult:
        """Step 19's reconciliation query: after an AMBIGUOUS outcome
        (e.g. the create-call request timed out), the caller never
        received a provider_call_id, so it cannot use get_call_status.
        A real provider that supports idempotency keys exposes a way to
        look up "was a call already created for this key" -- this is
        that operation. Returns FAILED if no matching call is found."""
