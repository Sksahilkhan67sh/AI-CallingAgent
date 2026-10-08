"""Checkpoint 09 follow-up -- Dograh publishes NO call_status enum
("observed reason the call ended", docs.dograh.com/developer/webhooks),
so (CP13) classification is an exact table of the values Dograh's source can emit.
These tests pin every supported keyword AND the conservative fallback:
an unrecognized status must never be treated as a connected conversation
and must never skip recovery.
"""

import pytest

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationSession
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    NeverConnectedFailureReason,
    SuppressionSource,
)
from app.models.retry_policy import RetryPolicy
from app.services.phone import normalize_phone_number
from app.services.telephony.dograh_outcome import _VOCABULARY, classify_call_outcome

S = CallAttemptState

# Every `call_status` Dograh can emit, taken from its source (TelephonyCallStatus in
# dograh-hq/dograh api/enums.py + EndTaskReason in dograh-hq/pipecat). Source-verified, not
# live-verified. Pinning the whole list means a new Dograh value shows up as a failing test.
_ENDED = [
    "completed", "user_hangup", "end_call", "call_transferred", "transfer_call",
    "call_duration_exceeded", "user_idle_max_duration_exceeded",
]  # fmt: skip
_NEVER = ["busy", "no-answer", "voicemail_detected", "failed", "canceled", "error"]
_DROPPED = ["unexpected_error", "pipeline_error", "system_cancelled"]
_NON_FINAL = ["initiated", "ringing", "in-progress", "answered"]


def test_vocabulary_is_exactly_the_documented_dograh_values():
    documented = {s.replace("-", "_") for s in _ENDED + _NEVER + _DROPPED + _NON_FINAL}
    assert set(_VOCABULARY) == documented


@pytest.mark.parametrize("status", _ENDED)
def test_ended_values_are_a_finished_conversation(status):
    c = classify_call_outcome(status, None)
    assert (c.state, c.recognized, c.non_final) == (S.ENDED_NORMALLY, True, False)


@pytest.mark.parametrize("status", _NEVER)
def test_never_connected_values(status):
    c = classify_call_outcome(status, None)
    assert (c.state, c.recognized) == (S.FAILED_TO_CONNECT, True)
    assert c.never_connected_reason is not None


@pytest.mark.parametrize("status", _DROPPED)
def test_dropped_values(status):
    c = classify_call_outcome(status, None)
    assert (c.state, c.recognized) == (S.DROPPED_MID_CALL, True)
    assert c.mid_call_reason is not None


@pytest.mark.parametrize("status", _NON_FINAL)
def test_non_final_values_never_decide(status):
    assert classify_call_outcome(status, None).non_final is True


@pytest.mark.parametrize(
    "value",
    [
        None, "", "   ", "unknown", "fail", "failure", "cancelled", "incomplete",
        "unsuccessful", "some_new_dograh_value", "call_completed_ok",
    ],
)  # fmt: skip
def test_ambiguous_or_unknown_is_never_a_connected_conversation(value):
    c = classify_call_outcome(value, None)
    assert c.recognized is False
    assert c.state == S.FAILED_TO_CONNECT  # not ENDED_NORMALLY: never claim it connected
    assert c.never_connected_reason == NeverConnectedFailureReason.PROVIDER_ERROR


# -- end to end: the unrecognized path goes ONLY through RecoveryManager --


def _attempt(db_session, phone):
    campaign = Campaign(name="classification e2e", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    db_session.add(RetryPolicy(campaign_id=campaign.id))
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DIALING,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        provider="dograh",
        provider_call_id="77",
        state=CallAttemptState.INITIATED,
    )
    db_session.add(attempt)
    db_session.commit()
    return contact, attempt


def _post(client, attempt, run_id, status):
    return client.post(
        "/api/v1/webhooks/dograh/call-completed",
        json={"call_attempt_id": str(attempt.id), "workflow_run_id": run_id, "call_status": status},
        headers={"Authorization": f"Bearer {get_settings().dograh_webhook_secret}"},
    )


def test_unrecognized_status_is_audited_creates_no_session_and_uses_recovery(client, db_session):
    contact, attempt = _attempt(db_session, "555-961-0001")

    response = _post(client, attempt, 77, "mystery_status")

    assert response.status_code == 200
    assert response.json()["outcome"] == "never_connected"
    db_session.refresh(attempt)
    db_session.refresh(contact)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT  # never claimed connected
    # Recovery decided (it was not skipped); for an unknown status it decides NOT to redial,
    # because the call may well have happened. CP13 policy, see CHECKPOINT-13-NOTES.md.
    assert contact.status == ContactStatus.COMPLETED_PARTIAL
    assert (
        db_session.query(ConversationSession)
        .filter(ConversationSession.call_attempt_id == attempt.id)
        .count()
        == 0
    )
    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.entity_id == attempt.id,
            AuditLog.action == "call_attempt.never_connected_via_dograh_webhook",
        )
        .one()
    )
    assert audit.event_metadata["classification"] == "unrecognized"


def test_unrecognized_status_still_respects_suppression(client, db_session):
    from app.models.suppression import Suppression

    contact, attempt = _attempt(db_session, "555-961-0002")
    db_session.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opt-out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.commit()

    assert _post(client, attempt, 77, "mystery_status").status_code == 200
    db_session.refresh(contact)
    assert contact.status != ContactStatus.RETRY_SCHEDULED  # suppression always wins
