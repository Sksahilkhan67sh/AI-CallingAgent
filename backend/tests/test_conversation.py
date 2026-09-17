"""ConversationSession, ConversationMessage, CallEvent persistence
(Checkpoint 01A Steps 11-12)."""

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent, ConversationMessage, ConversationSession
from app.models.enums import CallAttemptState, ConversationRole
from app.services.phone import normalize_phone_number


def _create_call_attempt(db_session) -> CallAttempt:
    campaign = Campaign(name="Conversation test campaign")
    db_session.add(campaign)
    db_session.flush()

    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-888-0000",
        normalized_phone_number=normalize_phone_number("555-888-0000"),
    )
    db_session.add(contact)
    db_session.flush()

    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.CONNECTED
    )
    db_session.add(attempt)
    db_session.flush()
    return attempt


def test_conversation_session_associates_with_call_attempt(db_session):
    attempt = _create_call_attempt(db_session)

    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()

    assert session.call_attempt_id == attempt.id
    assert session.status.value == "active"


def test_conversation_messages_preserve_chronological_order(db_session):
    attempt = _create_call_attempt(db_session)
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()

    db_session.add_all(
        [
            ConversationMessage(
                session_id=session.id, sequence=0, role=ConversationRole.AGENT,
                content="Hello, is this Alex?",
            ),
            ConversationMessage(
                session_id=session.id, sequence=1, role=ConversationRole.CONTACT,
                content="Speaking.",
            ),
        ]
    )
    db_session.flush()

    from sqlalchemy import select

    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.session_id == session.id)
        .order_by(ConversationMessage.sequence)
    )
    messages = list(db_session.execute(stmt).scalars())

    assert [m.sequence for m in messages] == [0, 1]
    assert messages[0].role == ConversationRole.AGENT
    assert messages[1].role == ConversationRole.CONTACT


def test_call_event_persists_with_payload(db_session):
    attempt = _create_call_attempt(db_session)

    event = CallEvent(
        call_attempt_id=attempt.id,
        event_type="connection.status",
        payload={"outcome": "connected"},
    )
    db_session.add(event)
    db_session.flush()

    assert event.id is not None
    assert event.payload == {"outcome": "connected"}
