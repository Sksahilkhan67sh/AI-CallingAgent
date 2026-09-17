"""Contact persistence. No business-policy decisions here -- see
app.services.contact_service for those."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import ContactStatus
from app.models.suppression import Suppression


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

    def list(
        self,
        *,
        campaign_id: uuid.UUID | None = None,
        status: ContactStatus | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[Contact], int]:
        """Deterministic order (created_at DESC, id DESC), bounded by
        limit/offset -- Checkpoint 02 Step 6."""
        filters = []
        if campaign_id is not None:
            filters.append(Contact.campaign_id == campaign_id)
        if status is not None:
            filters.append(Contact.status == status)

        total = self.db.execute(
            select(func.count()).select_from(Contact).where(*filters)
        ).scalar_one()

        stmt = (
            select(Contact)
            .where(*filters)
            .order_by(Contact.created_at.desc(), Contact.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self.db.execute(stmt).scalars())
        return items, total

    def campaign_counts(self, campaign_id: uuid.UUID) -> dict[str, int]:
        """Step 21 -- one aggregate query, not one query per contact."""
        stmt = (
            select(
                func.count().label("total"),
                func.count()
                .filter(
                    Contact.status != ContactStatus.CLOSED,
                    ~Contact.normalized_phone_number.in_(
                        select(Suppression.phone_number)
                    ),
                )
                .label("eligible"),
                func.count()
                .filter(
                    Contact.normalized_phone_number.in_(
                        select(Suppression.phone_number)
                    )
                )
                .label("suppressed"),
            )
            .select_from(Contact)
            .where(Contact.campaign_id == campaign_id)
        )
        row = self.db.execute(stmt).one()
        return {"total": row.total, "eligible": row.eligible, "suppressed": row.suppressed}
