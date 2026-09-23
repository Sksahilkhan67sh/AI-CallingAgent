"""RecoveryManager decision engine -- Checkpoint 05 §4-8."""

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import (
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
    SuppressionSource,
)
from app.models.retry_policy import RetryPolicy
from app.models.suppression import Suppression
from app.services.phone import normalize_phone_number
from app.services.recovery.manager import RecoveryManager
from app.services.recovery.scheduler import RecoveryScheduler


def _setup(
    db_session, *, phone="555-990-0001", campaign_status=CampaignStatus.ACTIVE, with_policy=True
):
    campaign = Campaign(name="Recovery test", status=campaign_status)
    db_session.add(campaign)
    db_session.flush()
    if with_policy:
        db_session.add(RetryPolicy(campaign_id=campaign.id))
        db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DISCONNECTED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.DROPPED_MID_CALL
    )
    db_session.add(attempt)
    db_session.flush()
    return campaign, contact, attempt


def test_retryable_reason_schedules_a_retry(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session)
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is True
    assert decision.delay_seconds == 30  # retry #1 spacing
    assert decision.next_attempt_number == 2
    assert contact.status == ContactStatus.RETRY_SCHEDULED
    assert redis_client.zcard("recovery:scheduled") == 1


def test_second_retry_uses_second_spacing_value(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0002")
    attempt.attempt_number = 2
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.NETWORK_PROBLEM.value,
    )

    assert decision.delay_seconds == 600  # retry #2 spacing
    assert decision.next_attempt_number == 3


def test_max_attempts_reached_is_terminal(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0003")
    attempt.attempt_number = 3  # already at max_retries(2) + 1 = 3 total attempts
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is False
    assert decision.reason == "max_attempts_reached"
    assert contact.status == ContactStatus.COMPLETED_PARTIAL
    assert redis_client.zcard("recovery:scheduled") == 0


def test_customer_hangup_does_not_retry_by_default(db_session, redis_client):
    """§7: 'do not automatically treat every customer hangup as a
    technical failure' -- the existing DEFAULT_MID_CALL_RULES already
    has customer_hangup=False."""
    campaign, contact, attempt = _setup(db_session, phone="555-990-0004")
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.CUSTOMER_HANGUP.value,
    )

    assert decision.should_retry is False
    assert "reason_not_retryable" in decision.reason


def test_rejected_never_connected_does_not_retry(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0005")
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=True,
        reason_key=NeverConnectedFailureReason.REJECTED.value,
    )

    assert decision.should_retry is False


def test_suppressed_contact_never_retries_regardless_of_reason(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0006")
    db_session.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opted out",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is False
    assert decision.reason == "suppressed"


def test_closed_contact_never_retries(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0007")
    contact.status = ContactStatus.CLOSED
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is False
    assert decision.reason == "suppressed"
    assert contact.status == ContactStatus.CLOSED  # not relabeled


def test_completed_campaign_does_not_retry(db_session, redis_client):
    campaign, contact, attempt = _setup(
        db_session, phone="555-990-0008", campaign_status=CampaignStatus.ACTIVE
    )
    campaign.status = CampaignStatus.COMPLETED
    db_session.flush()
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is False
    assert decision.reason == "campaign_completed"


def test_no_retry_policy_configured_defaults_to_no_retry(db_session, redis_client):
    campaign, contact, attempt = _setup(db_session, phone="555-990-0009", with_policy=False)
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    assert decision.should_retry is False
    assert decision.reason == "no_retry_policy_configured"


def test_recovery_events_are_logged(db_session, redis_client):
    from sqlalchemy import select

    from app.models.conversation import CallEvent

    campaign, contact, attempt = _setup(db_session, phone="555-990-0010")
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    events = (
        db_session.execute(select(CallEvent).where(CallEvent.call_attempt_id == attempt.id))
        .scalars()
        .all()
    )
    assert any(e.event_type == "RECOVERY_SCHEDULED" for e in events)


def test_recovery_actions_are_audited(db_session, redis_client):
    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    campaign, contact, attempt = _setup(db_session, phone="555-990-0011")
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=False,
        reason_key=MidCallDisconnectReason.TECHNICAL_ISSUE.value,
    )

    rows = (
        db_session.execute(select(AuditLog).where(AuditLog.action == "recovery.scheduled"))
        .scalars()
        .all()
    )
    assert any(str(r.entity_id) == str(attempt.id) for r in rows)
