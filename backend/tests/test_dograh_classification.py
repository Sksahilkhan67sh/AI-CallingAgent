"""Checkpoint 09 follow-up -- Dograh publishes NO call_status enum
("observed reason the call ended", docs.dograh.com/developer/webhooks),
so classification is an explicit, small, visible keyword heuristic.
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
from app.services.telephony.dograh_webhook_service import (
    _DROPPED_MID_CALL_KEYWORDS,
    _NEVER_CONNECTED_KEYWORDS,
    _NORMAL_COMPLETION_TOKENS,
    _classify,
)

S = CallAttemptState


@pytest.mark.parametrize("keyword,reason", _NEVER_CONNECTED_KEYWORDS)
def test_every_never_connected_keyword(keyword, reason):
    c = _classify(f"call_{keyword}")
    assert (c.state, c.never_connected_reason, c.recognized) == (S.FAILED_TO_CONNECT, reason, True)


@pytest.mark.parametrize("keyword", _DROPPED_MID_CALL_KEYWORDS)
def test_every_dropped_mid_call_keyword(keyword):
    c = _classify(f"call_{keyword}")
    assert (c.state, c.recognized) == (S.DROPPED_MID_CALL, True)
    assert c.mid_call_reason is not None


@pytest.mark.parametrize("token", sorted(_NORMAL_COMPLETION_TOKENS))
@pytest.mark.parametrize("fmt", ["{}", "user_{}", "AGENT-{}", "{} by caller"])
def test_every_normal_token_matches_as_whole_token(token, fmt):
    c = _classify(fmt.format(token))
    assert (c.state, c.recognized) == (S.ENDED_NORMALLY, True)


@pytest.mark.parametrize("value", ["network_error_drop", "connection_drop"])
def test_network_keywords_map_to_network_problem(value):
    from app.models.enums import MidCallDisconnectReason

    c = _classify(value)
    assert c.state == S.DROPPED_MID_CALL
    assert c.mid_call_reason == MidCallDisconnectReason.NETWORK_PROBLEM


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "unknown",
        "fail",
        "failed",
        "failure",
        "canceled",
        "cancelled",
        "incomplete",  # must NOT match the "complete" token
        "unsuccessful",  # must NOT match the "success" token
        "some_new_dograh_value",
    ],
)
def test_ambiguous_or_unknown_is_never_a_connected_conversation(value):
    c = _classify(value)
    assert c.recognized is False
    assert c.state == S.FAILED_TO_CONNECT  # not ENDED_NORMALLY: never claim it connected
    assert c.never_connected_reason == NeverConnectedFailureReason.PROVIDER_ERROR


@pytest.mark.parametrize("value", ["completed_with_error", "call completed, timeout"])
def test_failure_keywords_outrank_a_normal_token(value):
    assert _classify(value).state == S.DROPPED_MID_CALL


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


def test_unrecognized_status_is_audited_creates_no_session_and_uses_recovery(
    client, db_session
):
    contact, attempt = _attempt(db_session, "555-961-0001")

    response = _post(client, attempt, 2001, "mystery_status")

    assert response.status_code == 200
    assert response.json()["outcome"] == "never_connected"
    db_session.refresh(attempt)
    db_session.refresh(contact)
    assert attempt.state == CallAttemptState.FAILED_TO_CONNECT  # never claimed connected
    assert contact.status == ContactStatus.RETRY_SCHEDULED  # recovery was NOT skipped
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

    assert _post(client, attempt, 2002, "mystery_status").status_code == 200
    db_session.refresh(contact)
    assert contact.status != ContactStatus.RETRY_SCHEDULED  # suppression always wins
