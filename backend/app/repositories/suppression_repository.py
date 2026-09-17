"""Suppression persistence. Eligibility policy belongs to a later
checkpoint -- this repository only reads/writes rows."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.suppression import Suppression


class SuppressionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def is_suppressed(self, normalized_phone_number: str) -> bool:
        stmt = select(Suppression.contact_id).where(
            Suppression.phone_number == normalized_phone_number
        )
        return self.db.execute(stmt).first() is not None

    def add(self, suppression: Suppression) -> Suppression:
        self.db.add(suppression)
        self.db.flush()
        return suppression
