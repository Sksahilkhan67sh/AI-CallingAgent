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
from app.models.enums import ContactStatus
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.contact import ContactCreate, ContactUpdate
from app.services.audit_service import record_audit_event
from app.services.phone import InvalidPhoneNumberError, normalize_phone_number

# Placeholder actor until Checkpoint 02's Step 29 note is resolved by a
# real auth system: "keep endpoint design ready for it without creating
# fake authentication."
_ACTOR = "api-client"


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

        normalized = self._validate_and_normalize(data.phone_number)

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
        contact = self.contacts.add(contact)
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="contact.created",
            entity_type="contact",
            entity_id=contact.id,
            metadata={"campaign_id": str(contact.campaign_id)},
        )
        return contact

    def get_contact(self, contact_id: uuid.UUID) -> Contact:
        contact = self.contacts.get_by_id(contact_id)
        if contact is None:
            raise NotFoundError(f"Contact {contact_id} not found")
        return contact

    def list_contacts(
        self,
        *,
        campaign_id: uuid.UUID | None,
        status: ContactStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Contact], int]:
        return self.contacts.list(
            campaign_id=campaign_id, status=status, limit=limit, offset=offset
        )

    def update_contact(self, contact_id: uuid.UUID, data: ContactUpdate) -> Contact:
        contact = self.get_contact(contact_id)
        changed = False

        if data.phone_number is not None:
            normalized = self._validate_and_normalize(data.phone_number)
            if normalized != contact.normalized_phone_number:
                existing = self.contacts.get_by_campaign_and_normalized_phone(
                    contact.campaign_id, normalized
                )
                if existing is not None and existing.id != contact.id:
                    raise ConflictError(
                        f"Contact with phone number {normalized} already exists "
                        f"in campaign {contact.campaign_id}"
                    )
                contact.phone_number = data.phone_number
                contact.normalized_phone_number = normalized
                changed = True

        self.db.flush()
        if changed:
            record_audit_event(
                self.db,
                actor=_ACTOR,
                action="contact.updated",
                entity_type="contact",
                entity_id=contact.id,
            )
        return contact

    def deactivate_contact(self, contact_id: uuid.UUID) -> Contact:
        """Step 8 -- soft-deactivation. Never physically deletes a
        contact: call history, audit records, and campaign association
        must remain intact."""
        contact = self.get_contact(contact_id)
        contact.status = ContactStatus.CLOSED
        self.db.flush()
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="contact.deactivated",
            entity_type="contact",
            entity_id=contact.id,
        )
        return contact

    def _validate_and_normalize(self, phone_number: str) -> str:
        try:
            return normalize_phone_number(phone_number)
        except InvalidPhoneNumberError as exc:
            raise ValidationError(str(exc)) from exc
