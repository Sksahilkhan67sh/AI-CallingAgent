"""Checkpoint 09 §7 -- circuit breaker state transitions. Written
against the existing CircuitBreaker (app/services/telephony/
circuit_breaker.py), not a new implementation -- its TTL-based
recovery (the OPEN key expires after `open_seconds`, and the very next
call after that is treated as an implicit probe: `record_success`
clears the error count, `record_failure` reopens it immediately) *is*
this breaker's half-open mechanism -- a single-probe design rather
than a separately-named state, which is functionally equivalent to
what §7 asks for without a third state to maintain.
"""

from app.core.config import get_settings
from app.services.telephony.circuit_breaker import CircuitBreaker


def test_closed_by_default(redis_client):
    breaker = CircuitBreaker(redis_client, "test-provider-1")
    assert breaker.is_open() is False


def test_opens_after_error_threshold(redis_client):
    breaker = CircuitBreaker(redis_client, "test-provider-2")
    threshold = get_settings().circuit_breaker_error_threshold

    for _ in range(threshold - 1):
        breaker.record_failure()
    assert breaker.is_open() is False  # not yet at threshold

    breaker.record_failure()
    assert breaker.is_open() is True


def test_success_resets_the_error_count(redis_client):
    breaker = CircuitBreaker(redis_client, "test-provider-3")
    threshold = get_settings().circuit_breaker_error_threshold

    for _ in range(threshold - 1):
        breaker.record_failure()
    breaker.record_success()  # clears the count before it trips

    breaker.record_failure()
    assert breaker.is_open() is False  # back to 1 failure, well under threshold


def test_recovers_once_the_open_window_expires(redis_client):
    """The half-open probe: once the OPEN key's TTL expires, the very
    next call is let through. A success there closes the breaker
    again."""
    breaker = CircuitBreaker(redis_client, "test-provider-4")
    for _ in range(get_settings().circuit_breaker_error_threshold):
        breaker.record_failure()
    assert breaker.is_open() is True

    # Simulate the open_seconds TTL elapsing without a real sleep.
    redis_client.delete(f"circuit:{breaker.provider_name}:open")
    assert breaker.is_open() is False

    breaker.record_success()
    assert breaker.is_open() is False


def test_failed_probe_reopens_immediately(redis_client):
    """If the half-open probe itself fails, the breaker must reopen
    right away -- not wait for another full threshold's worth of
    failures."""
    breaker = CircuitBreaker(redis_client, "test-provider-5")
    for _ in range(get_settings().circuit_breaker_error_threshold):
        breaker.record_failure()
    redis_client.delete(f"circuit:{breaker.provider_name}:open")
    assert breaker.is_open() is False

    breaker.record_failure()  # the probe itself failed
    assert breaker.is_open() is True


def test_dograh_and_native_breakers_are_fully_independent(redis_client):
    dograh = CircuitBreaker(redis_client, "dograh")
    native = CircuitBreaker(redis_client, "mock")

    for _ in range(get_settings().circuit_breaker_error_threshold):
        dograh.record_failure()

    assert dograh.is_open() is True
    assert native.is_open() is False
