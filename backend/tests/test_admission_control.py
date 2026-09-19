"""Admission control: CPS, concurrency, backpressure, circuit breaker --
Checkpoint 03 Steps 8-11, 20, real Redis (Step 31)."""

from app.services.queue.admission_controller import AdmissionController
from app.services.telephony.circuit_breaker import CircuitBreaker


def _controller(redis_client, **overrides) -> AdmissionController:
    defaults = dict(
        global_cps_limit=100,
        campaign_cps_limit=100,
        provider_cps_limit=100,
        global_concurrency_limit=100,
        campaign_concurrency_limit=100,
        provider_concurrency_limit=100,
    )
    defaults.update(overrides)
    return AdmissionController(redis_client, **defaults)


def test_global_cps_limit_is_enforced(redis_client):
    controller = _controller(redis_client, global_cps_limit=2)

    results = [
        controller.try_admit(campaign_id="c1", provider_name="mock").admitted for _ in range(5)
    ]

    assert sum(results) == 2  # only the first 2 within this second are admitted


def test_campaign_cps_limit_is_isolated_per_campaign(redis_client):
    """Step 11: one campaign hitting its CPS limit must not affect another."""
    controller = _controller(redis_client, campaign_cps_limit=1, global_cps_limit=100)

    a1 = controller.try_admit(campaign_id="campaign-a", provider_name="mock")
    a2 = controller.try_admit(campaign_id="campaign-a", provider_name="mock")
    b1 = controller.try_admit(campaign_id="campaign-b", provider_name="mock")

    assert a1.admitted is True
    assert a2.admitted is False
    assert a2.reason == "cps_exceeded"
    assert b1.admitted is True


def test_provider_cps_limit_is_enforced(redis_client):
    controller = _controller(redis_client, provider_cps_limit=1, global_cps_limit=100)

    first = controller.try_admit(campaign_id="c1", provider_name="mock")
    second = controller.try_admit(campaign_id="c2", provider_name="mock")

    assert first.admitted is True
    assert second.admitted is False
    assert second.reason == "cps_exceeded"


def test_global_concurrency_limit_is_enforced(redis_client):
    controller = _controller(redis_client, global_concurrency_limit=1)

    first = controller.try_admit(campaign_id="c1", provider_name="mock")
    second = controller.try_admit(campaign_id="c2", provider_name="mock")

    assert first.admitted is True
    assert second.admitted is False
    assert second.reason == "concurrency_exceeded"


def test_campaign_concurrency_limit_is_isolated_per_campaign(redis_client):
    controller = _controller(
        redis_client, campaign_concurrency_limit=1, global_concurrency_limit=100
    )

    a1 = controller.try_admit(campaign_id="campaign-a", provider_name="mock")
    a2 = controller.try_admit(campaign_id="campaign-a", provider_name="mock")
    b1 = controller.try_admit(campaign_id="campaign-b", provider_name="mock")

    assert a1.admitted is True
    assert a2.admitted is False
    assert b1.admitted is True


def test_provider_concurrency_limit_is_enforced(redis_client):
    controller = _controller(
        redis_client, provider_concurrency_limit=1, global_concurrency_limit=100
    )

    first = controller.try_admit(campaign_id="c1", provider_name="mock")
    second = controller.try_admit(campaign_id="c2", provider_name="mock")

    assert first.admitted is True
    assert second.admitted is False


def test_release_frees_the_concurrency_slot(redis_client):
    controller = _controller(redis_client, global_concurrency_limit=1)

    first = controller.try_admit(campaign_id="c1", provider_name="mock")
    assert first.admitted is True

    controller.release(campaign_id="c1", provider_name="mock")

    second = controller.try_admit(campaign_id="c2", provider_name="mock")
    assert second.admitted is True  # slot was freed


def test_rejected_admission_rolls_back_partial_reservations(redis_client):
    """A concurrency-admitted-but-CPS-rejected attempt must not leave a
    leaked concurrency slot behind."""
    controller = _controller(redis_client, global_concurrency_limit=100, campaign_cps_limit=1)

    controller.try_admit(campaign_id="c1", provider_name="mock")  # uses up campaign CPS
    controller.try_admit(campaign_id="c1", provider_name="mock")  # rejected on CPS

    # global concurrency should reflect only the ONE admitted call, not two
    assert int(redis_client.get("concurrency:global")) == 1


def test_backpressure_does_not_flood_when_capacity_is_exhausted(redis_client):
    """Step 10/35: many attempts against a tiny capacity -- admitted
    count never exceeds the configured limit, and rejected attempts are
    cleanly rejected rather than corrupting state."""
    controller = _controller(redis_client, global_concurrency_limit=3, global_cps_limit=1000)

    results = [
        controller.try_admit(campaign_id="c1", provider_name="mock").admitted for _ in range(50)
    ]

    assert sum(results) == 3


def test_circuit_breaker_opens_after_threshold_and_blocks_admission(redis_client):
    controller = _controller(redis_client)
    breaker = CircuitBreaker(redis_client, "mock")
    breaker.error_threshold = 3

    for _ in range(3):
        breaker.record_failure()

    assert breaker.is_open() is True
    result = controller.try_admit(campaign_id="c1", provider_name="mock")
    assert result.admitted is False
    assert result.reason == "circuit_open"


def test_circuit_breaker_success_resets_error_count(redis_client):
    breaker = CircuitBreaker(redis_client, "mock")
    breaker.error_threshold = 3

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert breaker.is_open() is False  # only 1 consecutive failure since the reset
