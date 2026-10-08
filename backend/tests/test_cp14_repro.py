"""CP14 -- reproductions of the five verified problems, written BEFORE any fix.

Each test asserts the CORRECT behaviour and therefore FAILS on develop @ 27997f5 (the CP13
merge). They exist to prove the problems are real and to prove, later, that the fixes close
them. Once the fixes land they are superseded by the proper CP14 suites and removed (the
baseline failure output is recorded in docs/CHECKPOINT-14-NOTES.md).

Only APIs that exist on the baseline are used; no phone number here is a real one.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, SuppressionSource
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.services.eligibility_service import DialEligibilityService
from app.services.phone import InvalidPhoneNumberError, normalize_phone_number
from app.services.queue.admission_controller import AdmissionController
from app.services.queue.dialer_worker import JobOutcome, process_one_job
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from app.services.telephony.circuit_breaker import CircuitBreaker

IST = ZoneInfo("Asia/Kolkata")
_PHONE = "+919812345601"  # syntactically valid Indian mobile; not a real subscriber


# -- C4: phone normalization ---------------------------------------------------------------------


def test_c4_bare_ten_digit_indian_mobile_is_not_treated_as_us():
    assert normalize_phone_number("9876543210") == "+919876543210"


def test_c4_trunk_zero_is_stripped_not_kept():
    assert normalize_phone_number("098765 43210") == "+919876543210"


def test_c4_numbers_over_fifteen_digits_are_rejected():
    with pytest.raises(InvalidPhoneNumberError):
        normalize_phone_number("+1234567890123456")  # 16 digits: not E.164


# -- C5a: calling window ---------------------------------------------------------------------------


def _setup(db_session, *, policy: RetryPolicy | None = None):
    campaign = Campaign(name="cp14 repro", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=_PHONE,
        normalized_phone_number=_PHONE,
        status=ContactStatus.PENDING,
    )
    db_session.add(contact)
    if policy is not None:
        policy.campaign_id = campaign.id
        db_session.add(policy)
    db_session.flush()
    return campaign, contact


def _check(db_session, policy, now):
    campaign, contact = _setup(db_session, policy=policy)
    return DialEligibilityService(SuppressionRepository(db_session)).check(
        contact, campaign, policy, now=now
    )


def test_c5a_window_is_read_in_local_time_inside(db_session):
    # 04:30 UTC is 10:00 in Asia/Kolkata: a 10:00-18:00 window is OPEN.
    now = datetime(2026, 1, 5, 4, 30, tzinfo=UTC)
    result = _check(db_session, RetryPolicy(window_start=time(10), window_end=time(18)), now)
    assert result.eligible is True


def test_c5a_window_is_read_in_local_time_outside(db_session):
    # 13:00 UTC is 18:30 in Asia/Kolkata: the same window is CLOSED (UTC says open).
    now = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
    result = _check(db_session, RetryPolicy(window_start=time(10), window_end=time(18)), now)
    assert result.eligible is False


def test_c5a_overnight_window_matches(db_session):
    # 18:30 UTC is 00:00 IST: inside a 22:00-06:00 overnight window.
    now = datetime(2026, 1, 5, 18, 30, tzinfo=UTC)
    result = _check(db_session, RetryPolicy(window_start=time(22), window_end=time(6)), now)
    assert result.eligible is True


def _closed_hour_everywhere() -> tuple[time, time]:
    """A one-hour window that excludes both the current UTC time and the current IST time."""
    now = datetime.now(UTC)
    blocked = {now.hour, now.astimezone(IST).hour}
    for hour in range(0, 22):
        if not ({hour, hour + 1} & blocked):
            return time(hour), time(hour + 1)
    raise AssertionError("no closed window found")  # pragma: no cover


def _queue(redis_client) -> RedisStreamQueue:
    return RedisStreamQueue(redis_client, "test:cp14:calls", "test:cp14:workers")


def _admission(redis_client) -> AdmissionController:
    return AdmissionController(
        redis_client,
        global_cps_limit=1000,
        campaign_cps_limit=1000,
        provider_cps_limit=1000,
        global_concurrency_limit=1000,
        campaign_concurrency_limit=1000,
        provider_concurrency_limit=1000,
    )


def test_c5a_first_attempt_job_outside_window_is_not_lost(db_session, redis_client, provider):
    start, end = _closed_hour_everywhere()
    campaign, contact = _setup(db_session, policy=RetryPolicy(window_start=start, window_end=end))
    queue = _queue(redis_client)
    queue.enqueue(DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1))

    outcome = process_one_job(
        db_session,
        queue,
        _admission(redis_client),
        provider,
        CircuitBreaker(redis_client, provider.name),
        consumer_name="w1",
        block_ms=50,
    )
    db_session.commit()

    still_in_stream = redis_client.xpending("test:cp14:calls", "test:cp14:workers")["pending"]
    scheduled = redis_client.zcard("recovery:scheduled")
    assert outcome != JobOutcome.ADMITTED_AND_DIALED
    # The job must exist somewhere durable-enough to be dialed later. On the baseline the
    # outcome is NOT_ELIGIBLE, which is ACKED: it exists nowhere (the contact stays PENDING).
    assert still_in_stream + scheduled >= 1, f"job lost (outcome={outcome})"


# -- C5b: retry policy / 24x7 ----------------------------------------------------------------------


def test_c5b_creating_a_campaign_creates_its_retry_policy(client, db_session):
    response = client.post("/api/v1/campaigns", json={"name": "policy at birth"})
    assert response.status_code == 201
    rows = db_session.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == response.json()["id"])
    ).all()
    assert len(rows) == 1


def test_c5b_campaign_without_a_policy_row_is_not_callable_24x7(db_session):
    # 21:30 UTC is 03:00 IST: no sane default window allows a call then.
    campaign, contact = _setup(db_session, policy=None)
    result = DialEligibilityService(SuppressionRepository(db_session)).check(
        contact, campaign, None, now=datetime(2026, 1, 5, 21, 30, tzinfo=UTC)
    )
    assert result.eligible is False


# -- DNC -------------------------------------------------------------------------------------------


def test_dnc_a_number_without_a_contact_can_be_stored(db_session):
    db_session.add(
        Suppression(
            contact_id=None,
            phone_number="+919812345699",
            reason="registry list",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()
    assert SuppressionRepository(db_session).is_suppressed("+919812345699")


def test_dnc_admin_api_can_add_a_number(client):
    response = client.post("/api/v1/admin/suppressions", json={"phone_number": "+919812345698"})
    assert response.status_code in (200, 201), response.text


# -- Spend -----------------------------------------------------------------------------------------


def test_spend_a_daily_cap_of_zero_blocks_all_dialing(
    db_session, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("DAILY_DIAL_CAP", "0")
    get_settings.cache_clear()
    try:
        # Use a window-free path: no policy row on the baseline == 24x7.
        campaign, contact = _setup(db_session, policy=None)
        queue = _queue(redis_client)
        queue.enqueue(
            DialJob.new(campaign_id=campaign.id, contact_id=contact.id, attempt_number=1)
        )
        outcome = process_one_job(
            db_session,
            queue,
            _admission(redis_client),
            provider,
            CircuitBreaker(redis_client, provider.name),
            consumer_name="w1",
            block_ms=50,
        )
        attempts = db_session.execute(
            select(CallAttempt).where(CallAttempt.contact_id == contact.id)
        ).all()
        assert outcome != JobOutcome.ADMITTED_AND_DIALED
        assert attempts == []
    finally:
        monkeypatch.delenv("DAILY_DIAL_CAP")
        get_settings.cache_clear()


def test_spend_estimate_inputs_are_not_persisted():
    # The webhook payload carries duration_seconds but no column stores it, so an estimated
    # spend cannot be computed from the database.
    assert hasattr(CallAttempt, "duration_seconds")


_ = timedelta  # (kept for readability of the window helpers above)
