"""Campaign/contact association -- Checkpoint 02 Steps 13-15, 20-21.

Kept as its own service (rather than folded into ContactService or
CampaignService) because it composes both plus eligibility/suppression,
and neither owning service should have to reach into the other's
repository. See docs/CHECKPOINT-02-NOTES.md for why "add"/"remove" are
implemented as reassignment/soft-close under the existing direct-FK
schema rather than a join table.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.contact import Contact
from app.models.enums import ContactStatus
from app.repositories.campaign_repository import CampaignRepository
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.services.audit_service import record_audit_event
from app.services.eligibility_service import CampaignEligibilityService

_ACTOR = "api-client"


class CampaignContactService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.campaigns = CampaignRepository(db)
        self.contacts = ContactRepository(db)
        self.suppressions = SuppressionRepository(db)
        self.eligibility = CampaignEligibilityService(self.suppressions)

    def associate(self, campaign_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
        campaign = self.campaigns.get_by_id(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")

        contact = self.contacts.get_by_id(contact_id)
        if contact is None:
            raise NotFoundError(f"Contact {contact_id} not found")

        result = self.eligibility.check(contact, campaign)
        if not result.eligible:
            raise ConflictError(result.reason or "Contact is not eligible for campaign")

        if contact.campaign_id != campaign_id:
            existing = self.contacts.get_by_campaign_and_normalized_phone(
                campaign_id, contact.normalized_phone_number
            )
            if existing is not None and existing.id != contact.id:
                raise ConflictError(
                    f"A contact with phone number {contact.normalized_phone_number} "
                    f"already exists in campaign {campaign_id}"
                )

            old_campaign_id = contact.campaign_id
            contact.campaign_id = campaign_id
            contact.status = ContactStatus.PENDING
            self.db.flush()
            record_audit_event(
                self.db,
                actor=_ACTOR,
                action="campaign.contact_added",
                entity_type="contact",
                entity_id=contact.id,
                metadata={
                    "from_campaign_id": str(old_campaign_id),
                    "to_campaign_id": str(campaign_id),
                },
            )

        return contact

    def remove(self, campaign_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
        campaign = self.campaigns.get_by_id(campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")

        contact = self.contacts.get_by_id(contact_id)
        if contact is None:
            raise NotFoundError(f"Contact {contact_id} not found")

        if contact.campaign_id != campaign_id:
            raise NotFoundError(
                f"Contact {contact_id} is not associated with campaign {campaign_id}"
            )

        contact.status = ContactStatus.CLOSED
        self.db.flush()
        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="campaign.contact_removed",
            entity_type="contact",
            entity_id=contact.id,
            metadata={"campaign_id": str(campaign_id)},
        )
        return contact

    def counts(self, campaign_id: uuid.UUID) -> dict[str, int]:
        if self.campaigns.get_by_id(campaign_id) is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return self.contacts.campaign_counts(campaign_id)
