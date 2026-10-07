"""Suppression persistence. Eligibility policy belongs to a later
checkpoint -- this repository only reads/writes rows."""

from collections.abc import Collection

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

    def suppressed_among(self, normalized_phone_numbers: Collection[str]) -> set[str]:
        """The subset of the given numbers that is suppressed: one query for a whole page
        instead of one per contact. Same match rule as is_suppressed."""
        if not normalized_phone_numbers:
            return set()
        stmt = select(Suppression.phone_number).where(
            Suppression.phone_number.in_(normalized_phone_numbers)
        )
        return set(self.db.execute(stmt).scalars())

    def add(self, suppression: Suppression) -> Suppression:
        self.db.add(suppression)
        self.db.flush()
        return suppression
