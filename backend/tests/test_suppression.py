"""Suppression persistence -- Database Design §2.12."""

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import SuppressionSource
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.services.phone import normalize_phone_number


def test_suppression_persists_and_is_queryable_by_phone_number(db_session):
    campaign = Campaign(name="Suppression persistence test")
    db_session.add(campaign)
    db_session.flush()

    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-666-7777",
        normalized_phone_number=normalize_phone_number("555-666-7777"),
    )
    db_session.add(contact)
    db_session.flush()

    repo = SuppressionRepository(db_session)
    assert repo.is_suppressed(contact.normalized_phone_number) is False

    repo.add(
        Suppression(
            contact_id=contact.id,
            phone_number=contact.normalized_phone_number,
            reason="opted out during call",
            source=SuppressionSource.AGENT_IN_CALL,
        )
    )

    assert repo.is_suppressed(contact.normalized_phone_number) is True
