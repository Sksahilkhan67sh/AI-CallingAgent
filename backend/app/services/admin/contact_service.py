"""Contacts for the admin dashboard -- Checkpoint 07 §13-14.

Reuses ContactService for list/get; adds phone masking (never expose
full numbers in a list view -- §13) and the suppression indicator,
which ContactResponse (the existing public schema) doesn't carry.
"""

import uuid

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.admin import ContactDetail, ContactListItem
from app.services.contact_service import ContactService
from app.services.phone import mask_phone_number


def list_contacts(
    db: Session, *, campaign_id=None, status=None, limit: int, offset: int
) -> tuple[list[ContactListItem], int]:
    items, total = ContactService(db).list_contacts(
        campaign_id=campaign_id, status=status, limit=limit, offset=offset
    )
    suppressed_numbers = SuppressionRepository(db).suppressed_among(
        {c.normalized_phone_number for c in items}
    )
    return [
        ContactListItem(
            id=c.id,
            campaign_id=c.campaign_id,
            phone_masked=mask_phone_number(c.normalized_phone_number),
            status=c.status,
            attempt_count=c.attempt_count,
            suppressed=c.normalized_phone_number in suppressed_numbers,
            created_at=c.created_at,
        )
        for c in items
    ], total


def get_contact_detail(db: Session, contact_id: uuid.UUID) -> ContactDetail:
    contact = ContactService(db).get_contact(contact_id)  # raises NotFoundError
    campaign = db.get(Campaign, contact.campaign_id)
    suppression = SuppressionRepository(db).get_by_phone(contact.normalized_phone_number)

    return ContactDetail(
        id=contact.id,
        campaign_id=contact.campaign_id,
        campaign_name=campaign.name if campaign else "",
        phone_masked=mask_phone_number(contact.normalized_phone_number),
        status=contact.status,
        attempt_count=contact.attempt_count,
        suppressed=suppression is not None,
        suppression_reason=suppression.reason if suppression else None,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )
