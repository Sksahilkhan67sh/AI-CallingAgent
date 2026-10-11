"""Legal hold -- CP15A. A hold on a campaign or a contact blocks EVERY retention and erasure
action on the covered data. Setting or clearing one is admin-only, needs a reason, and is
audited (the reason goes to the audit row, which is itself kept while the hold applies)."""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.services.audit_service import record_audit_event


def held_contact_ids():
    """Subquery: ids of contacts covered by a hold (their own or their campaign's)."""
    return (
        select(Contact.id)
        .join(Campaign, Campaign.id == Contact.campaign_id)
        .where(or_(Contact.legal_hold.is_(True), Campaign.legal_hold.is_(True)))
    )


def held_campaign_ids():
    return select(Campaign.id).where(Campaign.legal_hold.is_(True))


def set_campaign_hold(
    db: Session, *, campaign_id: uuid.UUID, active: bool, reason: str, actor: str
) -> bool:
    campaign = db.get(Campaign, campaign_id, with_for_update=True)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} not found")
    changed = campaign.legal_hold != active
    campaign.legal_hold = active
    record_audit_event(
        db,
        actor=actor,
        action="legal_hold.set" if active else "legal_hold.cleared",
        entity_type="campaign",
        entity_id=campaign.id,
        metadata={"reason": reason, "changed": changed},
    )
    return changed


def set_contact_hold(
    db: Session, *, contact_id: uuid.UUID, active: bool, reason: str, actor: str
) -> bool:
    contact = db.get(Contact, contact_id, with_for_update=True)
    if contact is None:
        raise NotFoundError(f"Contact {contact_id} not found")
    changed = contact.legal_hold != active
    contact.legal_hold = active
    record_audit_event(
        db,
        actor=actor,
        action="legal_hold.set" if active else "legal_hold.cleared",
        entity_type="contact",
        entity_id=contact.id,
        metadata={"reason": reason, "changed": changed},
    )
    return changed
