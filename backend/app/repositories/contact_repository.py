"""Contact persistence. No business-policy decisions here -- see
app.services.contact_service for those."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact


class ContactRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, contact_id: uuid.UUID) -> Contact | None:
        return self.db.get(Contact, contact_id)

    def get_by_campaign_and_normalized_phone(
        self, campaign_id: uuid.UUID, normalized_phone_number: str
    ) -> Contact | None:
        stmt = select(Contact).where(
            Contact.campaign_id == campaign_id,
            Contact.normalized_phone_number == normalized_phone_number,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def add(self, contact: Contact) -> Contact:
        self.db.add(contact)
        self.db.flush()
        return contact
