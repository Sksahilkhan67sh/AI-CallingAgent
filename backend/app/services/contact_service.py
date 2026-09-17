"""Contact business logic.

Owns: phone normalization, campaign-scoped dedup, and the suppression
check on creation (a suppressed number must not be (re)added to active
calling -- Security Architecture §8, "suppression checked on every dial
including retries"; checking it at creation time too keeps a suppressed
number out of a campaign in the first place).
"""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.contact import Contact
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.contact import ContactCreate
from app.services.phone import InvalidPhoneNumberError, normalize_phone_number


class ContactService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.contacts = ContactRepository(db)
        self.campaigns = CampaignRepository(db)
        self.suppressions = SuppressionRepository(db)

    def create_contact(self, data: ContactCreate) -> Contact:
        campaign = self.campaigns.get_by_id(data.campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {data.campaign_id} not found")

        try:
            normalized = normalize_phone_number(data.phone_number)
        except InvalidPhoneNumberError as exc:
            raise ValidationError(str(exc)) from exc

        if self.suppressions.is_suppressed(normalized):
            raise ConflictError(
                f"Phone number {normalized} is suppressed and cannot be added"
            )

        existing = self.contacts.get_by_campaign_and_normalized_phone(
            data.campaign_id, normalized
        )
        if existing is not None:
            raise ConflictError(
                f"Contact with phone number {normalized} already exists in "
                f"campaign {data.campaign_id}"
            )

        contact = Contact(
            campaign_id=data.campaign_id,
            phone_number=data.phone_number,
            normalized_phone_number=normalized,
        )
        return self.contacts.add(contact)

    def get_contact(self, contact_id: uuid.UUID) -> Contact:
        contact = self.contacts.get_by_id(contact_id)
        if contact is None:
            raise NotFoundError(f"Contact {contact_id} not found")
        return contact
