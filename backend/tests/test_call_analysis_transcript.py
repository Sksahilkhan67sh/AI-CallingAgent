"""Checkpoint 06 §32.C -- transcript preparation."""

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationRole, ConversationSession
from app.models.enums import CallAttemptState, ContactStatus
from app.services.analysis.transcript import prepare_transcript
from app.services.phone import normalize_phone_number


def _session(db_session, phone="555-200-0001"):
    campaign = Campaign(name="CP06 transcript test")
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
    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.ENDED_NORMALLY
    )
    db_session.add(attempt)
    db_session.flush()
    session = ConversationSession(call_attempt_id=attempt.id)
    db_session.add(session)
    db_session.flush()
    return session


def _add(db_session, session, sequence, role, content):
    db_session.add(
        ConversationMessage(session_id=session.id, sequence=sequence, role=role, content=content)
    )
    db_session.flush()


def test_transcript_is_chronological(db_session):
    session = _session(db_session)
    _add(db_session, session, 2, ConversationRole.CONTACT, "second")
    _add(db_session, session, 1, ConversationRole.AGENT, "first")
    _add(db_session, session, 3, ConversationRole.AGENT, "third")

    transcript = prepare_transcript(db_session, session)

    assert transcript.lines == ["agent: first", "contact: second", "agent: third"]


def test_transcript_drops_empty_and_whitespace_only_messages(db_session):
    session = _session(db_session, phone="555-200-0002")
    _add(db_session, session, 1, ConversationRole.AGENT, "hello")
    _add(db_session, session, 2, ConversationRole.CONTACT, "   ")
    _add(db_session, session, 3, ConversationRole.AGENT, "")

    transcript = prepare_transcript(db_session, session)

    assert transcript.lines == ["agent: hello"]
    # message_count reflects the raw row count (used for cost/observability
    # accounting), not the post-cleanup line count.
    assert transcript.message_count == 3


def test_transcript_collapses_duplicate_whitespace(db_session):
    session = _session(db_session, phone="555-200-0003")
    _add(db_session, session, 1, ConversationRole.AGENT, "hello   there\n\nfriend")

    transcript = prepare_transcript(db_session, session)

    assert transcript.lines == ["agent: hello there friend"]


def test_transcript_never_mutates_original_messages(db_session):
    session = _session(db_session, phone="555-200-0004")
    _add(db_session, session, 1, ConversationRole.AGENT, "hello   there")

    prepare_transcript(db_session, session)

    stored = (
        db_session.query(ConversationMessage)
        .filter(ConversationMessage.session_id == session.id)
        .one()
    )
    assert stored.content == "hello   there"


def test_transcript_truncates_long_conversations_preserving_ends(db_session, monkeypatch):
    from app.core import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("ANALYSIS_MAX_TRANSCRIPT_MESSAGES", "10")
    config.get_settings.cache_clear()
    try:
        session = _session(db_session, phone="555-200-0005")
        for i in range(30):
            _add(db_session, session, i, ConversationRole.AGENT, f"line {i}")

        transcript = prepare_transcript(db_session, session)

        assert transcript.truncated is True
        assert len(transcript.lines) == 10
        assert transcript.lines[0] == "agent: line 0"  # beginning preserved
        assert transcript.lines[-1] == "agent: line 29"  # ending preserved
    finally:
        config.get_settings.cache_clear()
