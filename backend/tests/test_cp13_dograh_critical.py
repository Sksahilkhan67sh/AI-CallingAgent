"""CP13 -- Dograh critical fixes, against real PostgreSQL and Redis.

What this proves and what it does not. The Dograh HTTP boundary is a deterministic fake or a
monkeypatched `httpx.post`; nothing here is evidence about a LIVE Dograh instance. The status
vocabulary is taken from Dograh's source (see app/services/telephony/dograh_outcome.py).
Live validation is a separate gate, documented in docs/CHECKPOINT-13-NOTES.md.
"""

import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import ConversationSession
from app.models.enums import CallAttemptState, ContactStatus
from app.models.processed_event import ProcessedEvent
from app.models.suppression import Suppression
from app.schemas.dograh_webhook import DograhWebhookPayload
from app.services.queue.dialer_worker import JobOutcome
from app.services.queue.job import DialJob
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.factory import get_recovery_scheduler
from app.services.telephony.dograh_client import (
    DograhApiError,
    DograhClient,
    DograhConfigurationError,
    DograhErrorCategory,
    parse_retry_after,
)
from app.services.telephony.dograh_outcome import TriggerPolicy, classify_trigger_error
from app.services.telephony.dograh_webhook_service import process_dograh_webhook
from tests.test_failure_injection import (
    AMBIGUOUS,
    FakeDograh,
    _admission,
    _attempts,
    _drive,
    _events,
    _queue,
    _Session,
    _world,
)

C = DograhErrorCategory
SCHEDULE_KEY = "recovery:scheduled"
LEASE_KEY = "concurrency:lease:global"
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


# -- fixtures / helpers ---------------------------------------------------------------------
@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("CALLING_ENGINE", "dograh")
    get_settings.cache_clear()
    f = FakeDograh()
    monkeypatch.setattr("app.services.telephony.factory.get_dograh_client", lambda: f)
    monkeypatch.setattr("app.services.queue.dialer_worker.time.sleep", lambda _s: None)
    yield f
    get_settings.cache_clear()


def _api_error(status: int, category: DograhErrorCategory, **kw) -> DograhApiError:
    return DograhApiError(status, "provider said no", category=category, **kw)


def _dial(fake, redis_client, provider, name, *, error=None, contact=None, attempt_number=1):
    """One real worker pass: queue -> admission -> dialer -> (fake) Dograh -> RecoveryManager."""
    if contact is None:
        campaign_id, (contact_id,) = _world()
    else:
        campaign_id, contact_id = contact
    queue, admission = _queue(redis_client, name), _admission(redis_client)
    fake.error = error
    queue.enqueue(
        DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=attempt_number)
    )
    message_id, job = queue.read_one("w1", 100)
    outcome = _drive(queue, admission, provider, redis_client, message_id, job)
    return outcome, (queue, admission, campaign_id, contact_id)


def _contact_status(contact_id):
    with _Session() as s:
        return s.get(Contact, contact_id).status


def _scheduled(redis_client) -> list[float]:
    """Seconds from now until each scheduled retry is due."""
    base = datetime.now(UTC).timestamp()
    return [score - base for _, score in redis_client.zrange(SCHEDULE_KEY, 0, -1, withscores=True)]


def _audit(entity_id, action) -> int:
    with _Session() as s:
        return s.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == entity_id, AuditLog.action == action)
        ).scalar_one()


def _due_retry(sc, seconds=45):
    queue, _admission_, _c, _k = sc
    n = dispatch_due_recovery_jobs(
        get_recovery_scheduler(), queue, now=datetime.now(UTC) + timedelta(seconds=seconds)
    )
    assert n == 1
    return queue.read_one("w2", 100)


# =============================================================================================
# 1. HTTP status classification, through the real client
# =============================================================================================
def _client(**kw) -> DograhClient:
    return DograhClient(
        base_url="https://dograh.example.com",
        api_key="dg_test_key",
        trigger_uuid="11111111-1111-1111-1111-111111111111",
        **kw,
    )


def _respond(monkeypatch, status, *, headers=None, json=None):
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **k: httpx.Response(
            status, headers=headers or {}, json=json or {"detail": "x"}
        ),
    )


# Verified against Dograh's route source (api/routes/public_agent.py): 401 bad key, 403 wrong
# org, 404 trigger missing/inactive, 400 telephony unconfigured or initiation failed, 402 quota,
# 409 workflow has no owner, 422 request validation, 429 concurrent-call limit.
POLICY = [
    (400, C.VALIDATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (401, C.AUTHENTICATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (402, C.VALIDATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (403, C.AUTHENTICATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (404, C.PROVIDER_REJECTED, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (408, C.AMBIGUOUS_REQUEST, TriggerPolicy.AMBIGUOUS, "provider_error"),
    (409, C.VALIDATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (422, C.VALIDATION_ERROR, TriggerPolicy.PERMANENT, "provider_configuration_error"),
    (429, C.RATE_LIMITED, TriggerPolicy.RATE_LIMITED, "provider_rate_limited"),
    (500, C.AMBIGUOUS_REQUEST, TriggerPolicy.AMBIGUOUS, "provider_error"),
    (502, C.AMBIGUOUS_REQUEST, TriggerPolicy.AMBIGUOUS, "provider_error"),
    (503, C.PROVIDER_UNAVAILABLE, TriggerPolicy.TRANSIENT, "provider_error"),
    (504, C.AMBIGUOUS_REQUEST, TriggerPolicy.AMBIGUOUS, "provider_error"),
]


@pytest.mark.parametrize("status,category,policy,reason_key", POLICY)
def test_http_status_maps_to_exactly_one_policy(monkeypatch, status, category, policy, reason_key):
    _respond(monkeypatch, status)
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert raised.value.category == category
    failure = classify_trigger_error(raised.value)
    assert (failure.policy, failure.reason_key) == (policy, reason_key)


def test_2xx_initiated_is_acceptance_not_an_outcome(monkeypatch):
    _respond(
        monkeypatch,
        200,
        json={"status": "initiated", "workflow_run_id": 4242, "workflow_run_name": "WR"},
    )
    result = _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert result.workflow_run_id == 4242
    # The client result carries no success/complete notion at all: the attempt state is
    # decided by the worker (INITIATED) and the webhook, never by this return value.
    assert not hasattr(result, "state") and not hasattr(result, "is_completed")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"status": "initiated"}),  # no workflow_run_id
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_malformed_or_runless_acceptance_is_ambiguous_never_success(monkeypatch, response):
    monkeypatch.setattr("httpx.post", lambda url, **k: response)
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert classify_trigger_error(raised.value).policy == TriggerPolicy.AMBIGUOUS


def test_network_timeout_is_ambiguous_and_connect_failure_is_not(monkeypatch):
    def read_timeout(url, **k):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("httpx.post", read_timeout)
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert classify_trigger_error(raised.value).policy == TriggerPolicy.AMBIGUOUS

    def refused(url, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("httpx.post", refused)
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert classify_trigger_error(raised.value).policy == TriggerPolicy.TRANSIENT


# =============================================================================================
# 2. Retry-After
# =============================================================================================
@pytest.mark.parametrize(
    "header,expected",
    [
        ("30", 30),
        ("0", 0),
        ("  45  ", 45),
        ("Wed, 07 Oct 2026 12:02:00 GMT", 120),  # HTTP-date, 2 minutes ahead of NOW
        ("Wed, 07 Oct 2026 11:00:00 GMT", None),  # in the past
        ("-5", None),
        ("abc", None),
        ("", None),
        ("1e3", None),
        ("30.5", None),
        ("99999999999", 3600),  # absurdly large -> clamped, never trusted
        ("Thu, 07 Oct 2027 12:00:00 GMT", 3600),  # an HTTP-date a year out is clamped too
        (None, None),
        ("9" * 500, None),  # oversized header
    ],
)
def test_retry_after_parsing_is_bounded_and_never_raises(header, expected):
    assert parse_retry_after(header, max_seconds=3600, now=NOW) == expected


def test_client_honors_retry_after_only_on_429(monkeypatch):
    _respond(monkeypatch, 429, headers={"Retry-After": "90"})
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert raised.value.retry_after_seconds == 90

    _respond(monkeypatch, 503, headers={"Retry-After": "90"})
    with pytest.raises(DograhApiError) as raised:
        _client().trigger_call(phone_number="+919899990001", initial_context={})
    assert raised.value.retry_after_seconds is None


def test_client_clamps_huge_retry_after_to_the_configured_ceiling(monkeypatch):
    _respond(monkeypatch, 429, headers={"Retry-After": "86400"})
    with pytest.raises(DograhApiError) as raised:
        _client(retry_after_max_seconds=600).trigger_call(
            phone_number="+919899990001", initial_context={}
        )
    assert raised.value.retry_after_seconds == 600


# =============================================================================================
# 3. Dialer end to end: initiated, retry timing, permanent errors, budget
# =============================================================================================
def test_initiated_leaves_the_attempt_in_progress_not_complete(fake, redis_client, provider):
    outcome, (_q, _a, _cid, contact_id) = _dial(fake, redis_client, provider, "init")
    (attempt,) = _attempts(contact_id)
    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert attempt.state == CallAttemptState.INITIATED  # accepted for dialing, nothing more
    assert attempt.provider_call_id == str(fake.run_ids[0])
    assert _contact_status(contact_id) == ContactStatus.DIALING
    assert _scheduled(redis_client) == []  # no retry, no terminal decision


def test_transient_provider_error_retries_with_the_policy_spacing(fake, redis_client, provider):
    _dial(fake, redis_client, provider, "t503", error=_api_error(503, C.PROVIDER_UNAVAILABLE))
    assert fake.calls == 1
    (due,) = _scheduled(redis_client)
    assert 25 <= due <= 40  # RetryPolicy default first spacing (30s), not "immediately"


@pytest.mark.parametrize(
    "retry_after,low,high",
    [
        (120, 115, 130),  # honored: longer than the policy spacing
        (5, 25, 40),  # never SHORTER than the policy spacing
        (0, 25, 40),
        (None, 25, 40),  # absent -> the configured fallback
        (10**9, 3590, 3610),  # untrusted and huge -> ceiling, even if a caller forgot to clamp
    ],
)
def test_429_waits_at_least_retry_after_but_never_a_tight_loop(
    fake, redis_client, provider, retry_after, low, high
):
    error = _api_error(429, C.RATE_LIMITED, retry_after_seconds=retry_after)
    outcome, (_q, _a, _cid, contact_id) = _dial(fake, redis_client, provider, "t429", error=error)
    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    (due,) = _scheduled(redis_client)
    assert low <= due <= high
    assert fake.calls == 1  # the worker did not retry inline
    assert _contact_status(contact_id) == ContactStatus.RETRY_SCHEDULED


def test_429_is_recorded_as_rate_limiting_not_as_a_generic_failure(fake, redis_client, provider):
    error = _api_error(429, C.RATE_LIMITED, retry_after_seconds=60)
    _dial(fake, redis_client, provider, "t429b", error=error)
    with _Session() as s:
        (attempt,) = s.execute(select(CallAttempt)).scalars().all()[-1:]
        payload = _events(attempt.id)["DOGRAH_TRIGGER_FAILED"]
        assert payload["reason_key"] == "provider_rate_limited"
        assert payload["retry_after_seconds"] == 60
        assert _audit(attempt.id, "dograh.provider_rate_limited") == 1


@pytest.mark.parametrize(
    "error",
    [
        _api_error(400, C.VALIDATION_ERROR),
        _api_error(401, C.AUTHENTICATION_ERROR),
        _api_error(402, C.VALIDATION_ERROR),
        _api_error(403, C.AUTHENTICATION_ERROR),
        _api_error(404, C.PROVIDER_REJECTED),
        _api_error(409, C.VALIDATION_ERROR),
        _api_error(422, C.VALIDATION_ERROR),
        DograhConfigurationError("DOGRAH_API_KEY and DOGRAH_TRIGGER_UUID must both be set"),
    ],
)
def test_permanent_provider_faults_are_never_retried(fake, redis_client, provider, error):
    _dial(fake, redis_client, provider, "perm", error=error)
    # (the DograhConfigurationError case raises at client construction, so nothing is triggered)
    assert fake.calls <= 1
    assert _scheduled(redis_client) == []  # RecoveryManager scheduled nothing
    with _Session() as s:
        contact = s.execute(select(Contact)).scalars().all()[-1]
        attempt = s.execute(
            select(CallAttempt).where(CallAttempt.contact_id == contact.id)
        ).scalar_one()
        assert contact.status == ContactStatus.COMPLETED_PARTIAL
        assert attempt.state == CallAttemptState.FAILED_TO_CONNECT
        assert _audit(attempt.id, "dograh.provider_configuration_error") == 1


def test_provider_error_budget_stops_retries_and_is_durable(
    fake, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("DOGRAH_PROVIDER_ERROR_MAX_RETRIES", "1")
    get_settings.cache_clear()
    unavailable = _api_error(503, C.PROVIDER_UNAVAILABLE)

    _o, sc = _dial(fake, redis_client, provider, "budget", error=unavailable)
    assert len(_scheduled(redis_client)) == 1  # failure #1 <= budget(1): retried

    queue, admission, _cid, contact_id = sc
    message_id, job = _due_retry(sc)
    fake.error = unavailable
    # a brand-new session per pass == a process restart: the count lives in PostgreSQL
    _drive(queue, admission, provider, redis_client, message_id, job)

    assert fake.calls == 2
    assert _scheduled(redis_client) == []  # failure #2 > budget(1): no third attempt
    assert _contact_status(contact_id) == ContactStatus.COMPLETED_PARTIAL
    second = _attempts(contact_id)[1]
    assert (
        _events(second.id)["RECOVERY_TERMINALIZED"]["reason"] == "provider_error_budget_exhausted"
    )


def test_budget_zero_means_provider_errors_are_never_retried(
    fake, redis_client, provider, monkeypatch
):
    monkeypatch.setenv("DOGRAH_PROVIDER_ERROR_MAX_RETRIES", "0")
    get_settings.cache_clear()
    _dial(fake, redis_client, provider, "b0", error=_api_error(503, C.PROVIDER_UNAVAILABLE))
    assert _scheduled(redis_client) == []


def test_customer_outcomes_do_not_spend_the_provider_error_budget(
    fake, redis_client, provider, monkeypatch
):
    """Attempt 1 ends in a customer no-answer; attempt 2 is the FIRST provider error. With a
    provider budget of 1 that failure must still be retried -- the no-answer is not the
    provider's fault and must not have used the budget up."""
    monkeypatch.setenv("DOGRAH_PROVIDER_ERROR_MAX_RETRIES", "1")
    get_settings.cache_clear()
    _o, sc = _dial(fake, redis_client, provider, "mix")
    queue, admission, _cid, contact_id = sc
    first = _attempts(contact_id)[0]
    assert _deliver_webhook(_payload(str(first.id), first.provider_call_id, "no-answer")) == (
        "never_connected"
    )
    assert len(_scheduled(redis_client)) == 1

    message_id, job = _due_retry(sc)
    fake.error = _api_error(503, C.PROVIDER_UNAVAILABLE)
    _drive(queue, admission, provider, redis_client, message_id, job)

    assert fake.calls == 2
    assert len(_scheduled(redis_client)) == 1  # retried: customer outcomes are not budgeted


def test_suppressed_contact_gets_no_retry_after_a_provider_error(fake, redis_client, provider):
    campaign_id, (contact_id,) = _world()
    with _Session() as s:
        contact = s.get(Contact, contact_id)
        from app.models.enums import SuppressionSource

        s.add(
            Suppression(
                contact_id=contact.id,
                phone_number=contact.normalized_phone_number,
                reason="opt-out",
                source=SuppressionSource.MANUAL_API,
            )
        )
        s.commit()
    outcome, _sc = _dial(
        fake,
        redis_client,
        provider,
        "supp",
        error=_api_error(503, C.PROVIDER_UNAVAILABLE),
        contact=(campaign_id, contact_id),
    )
    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert fake.calls == 0
    assert _scheduled(redis_client) == []


# =============================================================================================
# 4. Lease regression (CP12-A): a Dograh failure never leaks the concurrency lease
# =============================================================================================
@pytest.mark.parametrize(
    "error",
    [
        _api_error(503, C.PROVIDER_UNAVAILABLE),
        _api_error(429, C.RATE_LIMITED, retry_after_seconds=60),
        _api_error(401, C.AUTHENTICATION_ERROR),
        AMBIGUOUS,
    ],
)
def test_dograh_failures_release_the_concurrency_lease(fake, redis_client, provider, error):
    _dial(fake, redis_client, provider, "lease", error=error)
    assert redis_client.zcard(LEASE_KEY) == 0


def test_a_successful_trigger_also_releases_its_lease(fake, redis_client, provider):
    _dial(fake, redis_client, provider, "lease_ok")
    assert redis_client.zcard(LEASE_KEY) == 0


# =============================================================================================
# 5. Webhook: classification, non-final status, opt-out, idempotency, transitions
# =============================================================================================
def _call_world(*, state=CallAttemptState.INITIATED, run_id="99", phone_suffix=None):
    """Committed (not fixture-scoped) rows so threads and fresh sessions can see them."""
    import random

    from app.models.campaign import Campaign
    from app.models.enums import CampaignStatus
    from app.models.retry_policy import RetryPolicy
    from tests.phone_helpers import normalize_phone_number

    with _Session() as s:
        campaign = Campaign(name="cp13 webhook", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        s.add(RetryPolicy(campaign_id=campaign.id))
        phone = phone_suffix or f"989-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
        contact = Contact(
            campaign_id=campaign.id,
            phone_number=phone,
            normalized_phone_number=normalize_phone_number(phone),
            status=ContactStatus.DIALING,
        )
        s.add(contact)
        s.flush()
        attempt = CallAttempt(
            contact_id=contact.id,
            attempt_number=1,
            provider="dograh",
            provider_call_id=run_id,
            state=state,
        )
        s.add(attempt)
        s.commit()
        return str(contact.id), str(attempt.id)


_RUN = iter(range(9_100_000, 9_900_000))


def _fresh_world(**kw):
    run_id = str(next(_RUN))
    contact_id, attempt_id = _call_world(run_id=run_id, **kw)
    return contact_id, attempt_id, run_id


def _payload(attempt_id, run_id, status, **extra) -> DograhWebhookPayload:
    return DograhWebhookPayload(
        call_attempt_id=attempt_id, workflow_run_id=int(run_id), call_status=status, **extra
    )


def _deliver_webhook(payload) -> str:
    with _Session() as s:
        result = process_dograh_webhook(s, payload)
        s.commit()
        return result.outcome


def _suppression_count(contact_id) -> int:
    with _Session() as s:
        return s.execute(
            select(func.count())
            .select_from(Suppression)
            .where(Suppression.contact_id == contact_id)
        ).scalar_one()


def _processed_events(run_id) -> int:
    with _Session() as s:
        return s.execute(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(ProcessedEvent.event_id == f"dograh:{run_id}")
        ).scalar_one()


def test_end_call_is_a_finished_conversation_and_is_never_redialed(redis_client):
    """Regression for the C6 bug: `end_call` (the agent ending a normal call) used to be treated
    as a provider failure and retried -- a second call to someone who had just finished one."""
    contact_id, attempt_id, run_id = _fresh_world()
    assert _deliver_webhook(_payload(attempt_id, run_id, "end_call")) == "ended_normally"
    assert _contact_status(contact_id) == ContactStatus.COMPLETED
    assert _scheduled(redis_client) == []


def test_no_answer_hyphenated_is_a_customer_outcome_and_retries(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    assert _deliver_webhook(_payload(attempt_id, run_id, "no-answer")) == "never_connected"
    assert _contact_status(contact_id) == ContactStatus.RETRY_SCHEDULED
    assert len(_scheduled(redis_client)) == 1
    with _Session() as s:
        # recorded as the customer's no-answer, NOT as a provider error
        assert s.get(CallAttempt, attempt_id).connection_failure_reason.value == "no_answer"


def test_unrecognized_status_never_retries_and_never_claims_a_connection(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    assert _deliver_webhook(_payload(attempt_id, run_id, "brand_new_value")) == "never_connected"
    assert _contact_status(contact_id) == ContactStatus.COMPLETED_PARTIAL
    assert _scheduled(redis_client) == []
    with _Session() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ConversationSession)
                .where(ConversationSession.call_attempt_id == attempt_id)
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("status", ["initiated", "ringing", "in-progress", "answered"])
def test_non_final_status_takes_no_decision_and_does_not_burn_the_real_completion(
    redis_client, status
):
    contact_id, attempt_id, run_id = _fresh_world()
    assert _deliver_webhook(_payload(attempt_id, run_id, status)) == "non_final_ignored"
    with _Session() as s:
        assert s.get(CallAttempt, attempt_id).state == CallAttemptState.INITIATED
    assert _processed_events(run_id) == 0
    assert _scheduled(redis_client) == []
    # the genuine completion for the same run is still processed afterwards
    assert _deliver_webhook(_payload(attempt_id, run_id, "completed")) == "ended_normally"


# -- opt-out --------------------------------------------------------------------------------
def test_opt_out_disposition_persists_suppression_and_closes_the_contact(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    outcome = _deliver_webhook(
        _payload(attempt_id, run_id, "user_hangup", call_disposition="do_not_call")
    )
    assert outcome == "ended_normally"
    assert _suppression_count(contact_id) == 1
    assert _contact_status(contact_id) == ContactStatus.CLOSED  # not relabelled Completed
    assert _audit(contact_id, "dograh.opt_out_suppressed") == 1
    assert _scheduled(redis_client) == []


def test_opt_out_in_the_mapped_disposition_is_honoured(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    _deliver_webhook(
        _payload(
            attempt_id,
            run_id,
            "user_hangup",
            call_disposition="x",
            mapped_call_disposition="Do-Not-Call",
        )
    )
    assert _suppression_count(contact_id) == 1


def test_opt_out_on_a_failed_call_still_suppresses_and_schedules_no_retry(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    _deliver_webhook(_payload(attempt_id, run_id, "no-answer", call_disposition="do_not_call"))
    assert _suppression_count(contact_id) == 1
    assert _contact_status(contact_id) == ContactStatus.CLOSED
    assert _scheduled(redis_client) == []  # RecoveryManager saw suppression first


@pytest.mark.parametrize(
    "disposition",
    [
        "not_interested",
        "do_not_call_later",
        "no_do_not_call",
        "callback_requested",
        "qualified",
        "",
    ],
)
def test_only_the_exact_configured_code_suppresses(redis_client, disposition):
    contact_id, attempt_id, run_id = _fresh_world()
    _deliver_webhook(_payload(attempt_id, run_id, "user_hangup", call_disposition=disposition))
    assert _suppression_count(contact_id) == 0
    assert _contact_status(contact_id) == ContactStatus.COMPLETED


def test_transcript_text_alone_never_suppresses(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    _deliver_webhook(
        _payload(
            attempt_id,
            run_id,
            "user_hangup",
            gathered_context={"summary": "customer said do not call me again"},
        )
    )
    assert _suppression_count(contact_id) == 0


def test_opt_out_signal_is_configurable_and_can_be_disabled(redis_client, monkeypatch):
    monkeypatch.setenv("DOGRAH_OPT_OUT_DISPOSITIONS", '["stop_calling"]')
    get_settings.cache_clear()
    try:
        c1, a1, r1 = _fresh_world()
        _deliver_webhook(_payload(a1, r1, "user_hangup", call_disposition="do_not_call"))
        assert _suppression_count(c1) == 0  # no longer the configured code
        c2, a2, r2 = _fresh_world()
        _deliver_webhook(_payload(a2, r2, "user_hangup", call_disposition="Stop Calling"))
        assert _suppression_count(c2) == 1
        monkeypatch.setenv("DOGRAH_OPT_OUT_DISPOSITIONS", "[]")
        get_settings.cache_clear()
        c3, a3, r3 = _fresh_world()
        _deliver_webhook(_payload(a3, r3, "user_hangup", call_disposition="stop_calling"))
        assert _suppression_count(c3) == 0
    finally:
        get_settings.cache_clear()


def test_future_work_for_an_opted_out_contact_never_dials(fake, redis_client, provider):
    """Durable suppression is authoritative: a queued/retry job that survives the opt-out is
    refused by the worker, whatever is still sitting in Redis."""
    campaign_id, (contact_id,) = _world()
    _dial(
        fake, redis_client, provider, "optq", error=_api_error(503, C.PROVIDER_UNAVAILABLE),
        contact=(campaign_id, contact_id),
    )  # fmt: skip
    assert len(_scheduled(redis_client)) == 1
    with _Session() as s:  # the customer opts out on a different, concurrent call
        from app.models.enums import SuppressionSource

        contact = s.get(Contact, contact_id)
        s.add(
            Suppression(
                contact_id=contact.id,
                phone_number=contact.normalized_phone_number,
                reason="opt-out via Dograh call disposition",
                source=SuppressionSource.AGENT_IN_CALL,
            )
        )
        contact.status = ContactStatus.CLOSED
        s.commit()

    queue = _queue(redis_client, "optq")
    n = dispatch_due_recovery_jobs(
        get_recovery_scheduler(), queue, now=datetime.now(UTC) + timedelta(seconds=45)
    )
    calls_before = fake.calls
    assert n == 0  # the dispatcher already refuses a suppressed contact...
    # ...and the WORKER's own durable check must independently refuse a job that reaches it
    # anyway (a message already sitting in Redis). Force that path.
    queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=contact_id, attempt_number=2))
    message_id, job = queue.read_one("w2", 100)
    outcome = _drive(queue, _admission(redis_client), provider, redis_client, message_id, job)
    assert outcome == JobOutcome.NOT_ELIGIBLE
    assert fake.calls == calls_before
    assert len(_attempts(contact_id)) == 1  # no second attempt row


# -- idempotency / concurrency ----------------------------------------------------------------
@pytest.mark.parametrize("deliveries", [1, 2, 5, 10])
def test_identical_webhook_delivered_n_times_changes_state_exactly_once(redis_client, deliveries):
    contact_id, attempt_id, run_id = _fresh_world()
    payload = _payload(attempt_id, run_id, "user_hangup", call_disposition="do_not_call")
    outcomes = [_deliver_webhook(payload) for _ in range(deliveries)]

    assert outcomes[0] == "ended_normally"
    assert set(outcomes[1:]) <= {"already_processed"}
    assert _suppression_count(contact_id) == 1
    assert _audit(contact_id, "dograh.opt_out_suppressed") == 1
    assert _processed_events(run_id) == 1
    # a duplicate is a no-op: it must not leave an audit trail of its own either
    assert _audit(attempt_id, "dograh.invalid_provider_transition") == 0
    with _Session() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ConversationSession)
                .where(ConversationSession.call_attempt_id == attempt_id)
            ).scalar_one()
            == 1
        )


def test_duplicate_failure_webhook_schedules_exactly_one_retry(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    payload = _payload(attempt_id, run_id, "no-answer")
    for _ in range(6):
        _deliver_webhook(payload)
    assert len(_scheduled(redis_client)) == 1


def test_concurrent_deliveries_of_one_opt_out_produce_one_logical_transition(redis_client):
    contact_id, attempt_id, run_id = _fresh_world()
    payload = _payload(attempt_id, run_id, "user_hangup", call_disposition="do_not_call")
    barrier, outcomes, errors = threading.Barrier(8), [], []

    def deliver():
        try:
            barrier.wait()
            outcomes.append(_deliver_webhook(payload))
        except Exception as exc:  # recorded and asserted below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert not errors, errors
    assert outcomes.count("ended_normally") == 1
    assert outcomes.count("already_processed") == 7
    assert _suppression_count(contact_id) == 1
    assert _audit(contact_id, "dograh.opt_out_suppressed") == 1
    assert _processed_events(run_id) == 1


def test_concurrent_opt_outs_via_two_attempts_still_leave_one_suppression_row(redis_client):
    """Two different runs for one contact both report opt-out: ON CONFLICT collapses them."""
    contact_id, attempt_id, run_id = _fresh_world()
    with _Session() as s:
        second = CallAttempt(
            contact_id=contact_id,
            attempt_number=2,
            provider="dograh",
            provider_call_id=str(next(_RUN)),
            state=CallAttemptState.INITIATED,
        )
        s.add(second)
        s.commit()
        second_id, second_run = str(second.id), second.provider_call_id
    _deliver_webhook(_payload(attempt_id, run_id, "user_hangup", call_disposition="do_not_call"))
    _deliver_webhook(_payload(second_id, second_run, "user_hangup", call_disposition="do_not_call"))
    assert _suppression_count(contact_id) == 1
    assert _audit(contact_id, "dograh.opt_out_suppressed") == 1  # only the inserter audits


def test_failed_suppression_write_rolls_the_whole_webhook_back(redis_client, monkeypatch):
    """PostgreSQL failure on the opt-out path: nothing is acknowledged as processed, so the
    provider's redelivery (or ours) can complete it. No false success, no lost opt-out."""
    contact_id, attempt_id, run_id = _fresh_world()
    payload = _payload(attempt_id, run_id, "user_hangup", call_disposition="do_not_call")

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr("app.services.telephony.dograh_webhook_service._suppress_for_opt_out", boom)
    with _Session() as s, pytest.raises(RuntimeError):
        try:
            process_dograh_webhook(s, payload)
        except RuntimeError:
            s.rollback()
            raise
    assert _processed_events(run_id) == 0
    with _Session() as s:
        assert s.get(CallAttempt, attempt_id).state == CallAttemptState.INITIATED
    monkeypatch.undo()

    assert _deliver_webhook(payload) == "ended_normally"  # the redelivery completes it
    assert _suppression_count(contact_id) == 1


# -- invalid transitions -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "terminal",
    [
        CallAttemptState.ENDED_NORMALLY,
        CallAttemptState.DROPPED_MID_CALL,
        CallAttemptState.FAILED_TO_CONNECT,
    ],
)
def test_stale_webhook_cannot_move_a_terminal_attempt(redis_client, terminal):
    contact_id, attempt_id = _call_world(state=terminal, run_id=str(next(_RUN)))
    before = _contact_status(contact_id)
    # a delivery with an event id we have never seen (no run id -> attempt-id identity)
    payload = DograhWebhookPayload(call_attempt_id=attempt_id, call_status="no-answer")
    assert _deliver_webhook(payload) == "already_processed"
    with _Session() as s:
        assert s.get(CallAttempt, attempt_id).state == terminal
    assert _contact_status(contact_id) == before
    assert _scheduled(redis_client) == []
    assert _audit(attempt_id, "dograh.invalid_provider_transition") == 1


def test_stale_webhook_cannot_reopen_a_closed_contact(redis_client):
    contact_id, attempt_id = _call_world(
        state=CallAttemptState.FAILED_TO_CONNECT, run_id=str(next(_RUN))
    )
    with _Session() as s:
        s.get(Contact, contact_id).status = ContactStatus.CLOSED
        s.commit()
    _deliver_webhook(DograhWebhookPayload(call_attempt_id=attempt_id, call_status="busy"))
    assert _contact_status(contact_id) == ContactStatus.CLOSED
    assert _scheduled(redis_client) == []


# =============================================================================================
# 6. Reconciliation and correlation
# =============================================================================================
def test_ambiguous_timeout_never_creates_a_second_call_before_reconciling(
    fake, redis_client, provider
):
    fake.runs = []
    _o, sc = _dial(fake, redis_client, provider, "rec1", error=AMBIGUOUS)
    assert (fake.calls, fake.reconcile_calls) == (1, 1)  # looked first, did not redial
    assert len(_scheduled(redis_client)) == 1  # the retry waits for RecoveryManager's spacing


def test_408_is_reconciled_not_blindly_retried(fake, redis_client, provider):
    fake.runs = []
    _dial(fake, redis_client, provider, "rec408", error=_api_error(408, C.AMBIGUOUS_REQUEST))
    assert fake.reconcile_calls == 1
    assert fake.calls == 1


def test_a_run_found_at_the_second_lookup_is_adopted_and_nobody_is_called_twice(
    fake, redis_client, provider
):
    fake.runs = []
    _o, sc = _dial(fake, redis_client, provider, "rec2", error=AMBIGUOUS)
    fake.error, fake.runs = None, [8_800_001]
    outcome = _drive(sc[0], sc[1], provider, redis_client, *_due_retry(sc))
    assert outcome == JobOutcome.RECONCILED
    assert fake.calls == 1


def test_no_run_at_either_lookup_allows_exactly_one_retry(fake, redis_client, provider):
    fake.runs = []
    _o, sc = _dial(fake, redis_client, provider, "rec3", error=AMBIGUOUS)
    fake.error = None
    outcome = _drive(sc[0], sc[1], provider, redis_client, *_due_retry(sc))
    assert outcome == JobOutcome.ADMITTED_AND_DIALED
    assert fake.calls == 2


def test_reconciliation_is_bounded_and_ends_without_a_dial(
    fake, redis_client, provider, monkeypatch
):
    """The lookup keeps failing. Deferral is allowed N times and then the retry is abandoned:
    no dial, no infinite polling, a durable audit trail, one expiry record."""
    monkeypatch.setenv("DOGRAH_RECONCILE_MAX_DEFERRALS", "3")
    get_settings.cache_clear()
    fake.runs = []
    _o, sc = _dial(fake, redis_client, provider, "rec4", error=AMBIGUOUS)
    queue, admission, _cid, contact_id = sc
    fake.error = None
    fake.reconcile_error = _api_error(503, C.PROVIDER_UNAVAILABLE)
    message_id, job = _due_retry(sc)

    outcomes = [_drive(queue, admission, provider, redis_client, message_id, job) for _ in range(5)]

    assert outcomes[:3] == [JobOutcome.RECONCILE_DEFERRED] * 3
    assert outcomes[3] == JobOutcome.RECONCILE_EXPIRED
    assert fake.calls == 1  # the customer was never dialed a second time
    first = _attempts(contact_id)[0]
    assert _audit(first.id, "dograh.reconciliation_deferred") == 3
    assert _audit(first.id, "dograh.reconciliation_expired") >= 1
    assert _contact_status(contact_id) == ContactStatus.COMPLETED_PARTIAL
    assert redis_client.zcard(LEASE_KEY) == 0


def test_attempt_id_is_sent_to_dograh_and_the_run_id_is_stored_against_the_attempt(
    fake, redis_client, provider, monkeypatch
):
    seen = {}
    real = fake.trigger_call

    def capture(*, phone_number, initial_context):
        seen.update(initial_context)
        return real(phone_number=phone_number, initial_context=initial_context)

    monkeypatch.setattr(fake, "trigger_call", capture)
    _o, (_q, _a, _cid, contact_id) = _dial(fake, redis_client, provider, "corr")
    (attempt,) = _attempts(contact_id)
    assert seen["call_attempt_id"] == str(attempt.id)
    assert attempt.provider_call_id == str(fake.run_ids[0])


def test_webhook_for_an_unknown_or_foreign_attempt_is_refused(redis_client):
    from app.services.telephony.dograh_webhook_service import DograhWebhookError

    contact_id, attempt_id, run_id = _fresh_world()
    with _Session() as s, pytest.raises(DograhWebhookError):
        process_dograh_webhook(s, _payload(attempt_id, int(run_id) + 1, "end_call"))
    assert _contact_status(contact_id) == ContactStatus.DIALING  # nothing moved


# =============================================================================================
# 7. Kill switch / pause regressions (the real coverage is the CP11/CP12-B suites; this pins
#    that CP13's failure paths did not reopen them)
# =============================================================================================
def test_kill_switch_still_blocks_a_retry_after_a_provider_error(
    fake, redis_client, provider, monkeypatch
):
    _o, sc = _dial(
        fake, redis_client, provider, "ks", error=_api_error(503, C.PROVIDER_UNAVAILABLE)
    )
    fake.error = None
    monkeypatch.setattr(
        "app.services.queue.dialer_worker.kill_switch.block_reason", lambda: "test_kill_switch"
    )
    outcome = _drive(sc[0], sc[1], provider, redis_client, *_due_retry(sc))
    assert outcome == JobOutcome.OUTBOUND_BLOCKED
    assert fake.calls == 1


def test_a_paused_campaign_still_holds_a_provider_error_retry(fake, redis_client, provider):
    from app.models.campaign import Campaign
    from app.models.enums import CampaignStatus

    _o, sc = _dial(
        fake, redis_client, provider, "pause", error=_api_error(503, C.PROVIDER_UNAVAILABLE)
    )
    fake.error = None
    later = datetime.now(UTC) + timedelta(seconds=45)
    with _Session() as s:
        s.get(Campaign, sc[2]).status = CampaignStatus.PAUSED
        s.commit()

    # CP12-B: the dispatcher holds (reschedules) the retry; it is neither dialed nor dropped
    assert dispatch_due_recovery_jobs(get_recovery_scheduler(), sc[0], now=later) == 0
    assert fake.calls == 1
    assert get_recovery_scheduler().pending_count() == 1

    with _Session() as s:
        s.get(Campaign, sc[2]).status = CampaignStatus.ACTIVE
        s.commit()
    resumed = later + timedelta(hours=1)
    assert dispatch_due_recovery_jobs(get_recovery_scheduler(), sc[0], now=resumed) == 1


# =============================================================================================
# 8. Configuration is validated at startup
# =============================================================================================
@pytest.mark.parametrize(
    "overrides",
    [
        {"dograh_retry_after_max_seconds": 0},
        {"dograh_retry_after_max_seconds": -1},
        {"dograh_provider_error_max_retries": -1},
        {"dograh_reconcile_max_deferrals": 0},
        {"dograh_opt_out_dispositions": ["do_not_call", "  "]},
    ],
)
def test_invalid_cp13_settings_are_rejected(overrides):
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        Settings(_env_file=None, **overrides)


def test_cp13_defaults_are_safe():
    s = Settings(_env_file=None)
    assert s.dograh_opt_out_dispositions == ["do_not_call"]
    assert 0 < s.dograh_retry_after_max_seconds <= 3600
    assert s.dograh_provider_error_max_retries >= 0
    assert s.dograh_reconcile_max_deferrals >= 1
