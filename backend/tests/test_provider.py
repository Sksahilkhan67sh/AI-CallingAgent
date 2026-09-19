"""Telephony provider adapter contract -- Checkpoint 03 Steps 22-28
(test numbering from the checkpoint's Step 30 list)."""

from app.models.enums import NeverConnectedFailureReason
from app.services.telephony.base import ProviderOutcome
from app.services.telephony.mock_provider import (
    MockTelephonyProvider,
    always_ambiguous,
    always_fails,
)


def test_successful_call_creation_returns_provider_call_id():
    provider = MockTelephonyProvider()

    result = provider.create_outbound_call(to_number="+15555550100", idempotency_key="k1")

    assert result.outcome == ProviderOutcome.INITIATED
    assert result.provider_call_id is not None


def test_provider_failure_returns_failure_reason():
    provider = MockTelephonyProvider()
    provider.set_outcome("+15555550101", always_fails(NeverConnectedFailureReason.BUSY))

    result = provider.create_outbound_call(to_number="+15555550101", idempotency_key="k2")

    assert result.outcome == ProviderOutcome.FAILED
    assert result.failure_reason == NeverConnectedFailureReason.BUSY


def test_ambiguous_outcome_is_distinct_from_failure():
    provider = MockTelephonyProvider()
    provider.set_outcome("+15555550102", always_ambiguous)

    result = provider.create_outbound_call(to_number="+15555550102", idempotency_key="k3")

    assert result.outcome == ProviderOutcome.AMBIGUOUS


def test_get_call_status_returns_the_recorded_call():
    provider = MockTelephonyProvider()
    created = provider.create_outbound_call(to_number="+15555550103", idempotency_key="k4")

    status = provider.get_call_status(created.provider_call_id)

    assert status.outcome == ProviderOutcome.INITIATED
    assert status.provider_call_id == created.provider_call_id


def test_get_call_status_for_unknown_id_returns_failed():
    provider = MockTelephonyProvider()

    status = provider.get_call_status("does-not-exist")

    assert status.outcome == ProviderOutcome.FAILED


def test_find_call_by_idempotency_key_finds_a_real_call():
    provider = MockTelephonyProvider()
    created = provider.create_outbound_call(to_number="+15555550104", idempotency_key="k5")

    found = provider.find_call_by_idempotency_key("k5")

    assert found.provider_call_id == created.provider_call_id


def test_find_call_by_idempotency_key_returns_failed_when_never_created():
    provider = MockTelephonyProvider()

    found = provider.find_call_by_idempotency_key("never-used-key")

    assert found.outcome == ProviderOutcome.FAILED
