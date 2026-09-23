"""Verifies enqueue_call_analysis is reached from the real
RecoveryManager._terminalize() call site -- exercising both the
mid-call-disconnect-with-conversation path (eligible) and the
never-connected path (must stay ineligible even though _terminalize
sets the same COMPLETED_PARTIAL contact status for both, §26-27).
"""

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import CallAttemptState, ContactStatus, MidCallDisconnectReason
from app.services.analysis.factory import get_analysis_queue
from app.services.phone import normalize_phone_number
from app.services.recovery.manager import RecoveryManager
from app.services.recovery.scheduler import RecoveryScheduler


def _setup(db_session, *, phone, attempt_state):
    campaign = Campaign(name="CP06 recovery wiring test")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.DISCONNECTED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(contact_id=contact.id, attempt_number=3, state=attempt_state)
    db_session.add(attempt)
    db_session.flush()
    return campaign, contact, attempt


def test_mid_call_disconnect_exhausted_retries_admits_analysis(db_session, redis_client):
    campaign, contact, attempt = _setup(
        db_session, phone="555-600-0001", attempt_state=CallAttemptState.DROPPED_MID_CALL
    )
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        ConversationMessage(
            session_id=session.id, sequence=1, role=ConversationRole.CONTACT, content="hello?"
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
    assert contact.status == ContactStatus.COMPLETED_PARTIAL
    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status.value == "pending"


def test_never_connected_exhausted_retries_does_not_admit_analysis(db_session, redis_client):
    """The dialer worker sets FAILED_TO_CONNECT before ever calling
    RecoveryManager for a never-connected outcome -- admission must
    refuse it even though _terminalize sets the same COMPLETED_PARTIAL
    contact status it would for a real mid-call disconnect."""
    campaign, contact, attempt = _setup(
        db_session, phone="555-600-0002", attempt_state=CallAttemptState.FAILED_TO_CONNECT
    )
    manager = RecoveryManager(db_session, RecoveryScheduler(redis_client))

    decision = manager.handle_disconnect(
        attempt,
        contact,
        campaign,
        never_connected=True,
        reason_key="rejected",
    )

    assert decision.should_retry is False
    queue = get_analysis_queue()
    assert redis_client.xlen(queue.stream_key) == 0
    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 0
    )
