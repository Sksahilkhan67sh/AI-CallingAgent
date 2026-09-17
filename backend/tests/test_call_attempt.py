"""Call attempt uniqueness -- Checkpoint 01 Step 6 idempotency invariant:
the DB must reject a duplicate (contact_id, attempt_number) pair."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState
from app.services.phone import normalize_phone_number


def _create_contact(db_session) -> Contact:
    campaign = Campaign(name="Attempt test campaign")
    db_session.add(campaign)
    db_session.flush()

    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-222-3333",
        normalized_phone_number=normalize_phone_number("555-222-3333"),
    )
    db_session.add(contact)
    db_session.flush()
    return contact


def test_call_attempt_can_be_created(db_session):
    contact = _create_contact(db_session)

    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.INITIATED
    )
    db_session.add(attempt)
    db_session.flush()

    assert attempt.id is not None
    assert attempt.state == CallAttemptState.INITIATED


def test_duplicate_attempt_number_for_same_contact_is_rejected(db_session):
    contact = _create_contact(db_session)

    db_session.add(
        CallAttempt(
            contact_id=contact.id, attempt_number=1, state=CallAttemptState.INITIATED
        )
    )
    db_session.flush()

    db_session.add(
        CallAttempt(
            contact_id=contact.id, attempt_number=1, state=CallAttemptState.INITIATED
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_attempt_number_allowed_for_different_contacts(db_session):
    contact_a = _create_contact(db_session)
    campaign = Campaign(name="Second campaign")
    db_session.add(campaign)
    db_session.flush()
    contact_b = Contact(
        campaign_id=campaign.id,
        phone_number="555-444-5555",
        normalized_phone_number=normalize_phone_number("555-444-5555"),
    )
    db_session.add(contact_b)
    db_session.flush()

    db_session.add(
        CallAttempt(
            contact_id=contact_a.id, attempt_number=1, state=CallAttemptState.INITIATED
        )
    )
    db_session.add(
        CallAttempt(
            contact_id=contact_b.id, attempt_number=1, state=CallAttemptState.INITIATED
        )
    )
    db_session.flush()  # must not raise


def test_recording_consent_defaults_to_null_not_granted(db_session):
    """Recording-Consent.md §6 guardrail: never default to granted."""
    contact = _create_contact(db_session)

    attempt = CallAttempt(
        contact_id=contact.id, attempt_number=1, state=CallAttemptState.INITIATED
    )
    db_session.add(attempt)
    db_session.flush()

    assert attempt.recording_consent is None
