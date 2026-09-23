"""Checkpoint 06 §32.A -- admission eligibility, and the admission-time
half of §32.B idempotency.
"""

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import CallAttemptState, ContactStatus
from app.services.analysis.admission import enqueue_call_analysis
from app.services.analysis.factory import get_analysis_queue
from app.services.phone import normalize_phone_number


def _setup(db_session, *, phone="555-100-0001", attempt_state=CallAttemptState.ENDED_NORMALLY):
    campaign = Campaign(name="CP06 admission test")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(contact_id=contact.id, attempt_number=1, state=attempt_state)
    db_session.add(attempt)
    db_session.flush()
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()
    db_session.add(
        ConversationMessage(
            session_id=session.id, sequence=1, role=ConversationRole.AGENT, content="Hi there"
        )
    )
    db_session.flush()
    return campaign, contact, attempt, session


def test_completed_call_is_queued_for_analysis(db_session, redis_client):
    campaign, contact, attempt, session = _setup(db_session)

    enqueue_call_analysis(db_session, attempt, contact)

    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status.value == "pending"
    assert analysis.conversation_session_id == session.id
    assert analysis.contact_id == contact.id
    assert analysis.campaign_id == campaign.id


def test_completed_partial_call_is_queued_for_analysis(db_session, redis_client):
    campaign, contact, attempt, session = _setup(
        db_session, phone="555-100-0002", attempt_state=CallAttemptState.DROPPED_MID_CALL
    )
    contact.status = ContactStatus.COMPLETED_PARTIAL
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)

    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status.value == "pending"


def test_non_terminal_call_is_not_queued(db_session, redis_client):
    campaign, contact, attempt, session = _setup(db_session, phone="555-100-0003")
    contact.status = ContactStatus.IN_CONVERSATION
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)

    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 0
    )


def test_never_connected_call_is_not_queued_even_if_contact_status_is_terminal(
    db_session, redis_client
):
    """§3/§26-27: FAILED_TO_CONNECT means no conversation ever happened --
    must never be analyzed, regardless of what the Contact's own status
    ends up as."""
    campaign, contact, attempt, session = _setup(
        db_session, phone="555-100-0004", attempt_state=CallAttemptState.FAILED_TO_CONNECT
    )
    # Even if some upstream bug left contact.status COMPLETED_PARTIAL for
    # a never-connected attempt, admission must still refuse it.
    contact.status = ContactStatus.COMPLETED_PARTIAL
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)

    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 0
    )


def test_closed_opted_out_call_is_not_queued(db_session, redis_client):
    """§26: Closed -> NO analysis, whether from never-connecting or from
    an opt-out during a connected call."""
    campaign, contact, attempt, session = _setup(db_session, phone="555-100-0005")
    contact.status = ContactStatus.CLOSED
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)

    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 0
    )


def test_duplicate_admission_does_not_create_a_second_analysis_row(db_session, redis_client):
    campaign, contact, attempt, session = _setup(db_session, phone="555-100-0006")

    enqueue_call_analysis(db_session, attempt, contact)
    enqueue_call_analysis(db_session, attempt, contact)  # simulates a second terminal-state path

    rows = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).all()
    )
    assert len(rows) == 1


def test_duplicate_admission_does_not_enqueue_a_second_redis_job(db_session, redis_client):
    campaign, contact, attempt, session = _setup(db_session, phone="555-100-0007")
    queue = get_analysis_queue()

    enqueue_call_analysis(db_session, attempt, contact)
    enqueue_call_analysis(db_session, attempt, contact)

    length = redis_client.xlen(queue.stream_key)
    assert length == 1


def test_admission_emits_a_call_event_and_audit_entry(db_session, redis_client):
    from app.models.audit_log import AuditLog
    from app.models.conversation import CallEvent

    campaign, contact, attempt, session = _setup(db_session, phone="555-100-0008")

    enqueue_call_analysis(db_session, attempt, contact)

    events = (
        db_session.query(CallEvent)
        .filter(
            CallEvent.call_attempt_id == attempt.id, CallEvent.event_type == "ANALYSIS_QUEUED"
        )
        .all()
    )
    assert len(events) == 1

    audits = (
        db_session.query(AuditLog).filter(AuditLog.action == "analysis.queued").all()
    )
    assert len(audits) == 1


def test_analysis_missing_conversation_session_is_still_admitted_with_null_session(
    db_session, redis_client
):
    """A defensive edge case -- eligibility depends on call_attempt/contact
    state, not on a ConversationSession existing; the worker is what
    handles a missing session (marks the analysis FAILED, §32.D)."""
    campaign = Campaign(name="no session")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-100-0009",
        normalized_phone_number=normalize_phone_number("555-100-0009"),
        status=ContactStatus.COMPLETED,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()

    enqueue_call_analysis(db_session, attempt, contact)

    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.conversation_session_id is None
