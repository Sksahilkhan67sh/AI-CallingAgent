"""Suppression persistence -- the single read/write path for the global do-not-call table.

CP14: every lookup canonicalizes the number through the shared normalizer first, so a
caller holding "+91 98765-43210", "09876543210" or the stored E.164 gets the same answer;
and every writer inserts with ON CONFLICT DO NOTHING, so concurrent opt-outs / imports of
the same number converge on one row without an error.
"""

import logging
import uuid
from collections.abc import Collection, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import SuppressionSource
from app.models.suppression import Suppression
from app.services.phone import InvalidPhoneError, normalize_phone

logger = logging.getLogger("suppression")


def canonical_number(number: str) -> str:
    """The key under which `number` is (or would be) suppressed. A value that does not parse
    (a legacy row the backfill has not fixed yet) is used as-is rather than dropped: an
    opt-out must never be lost because a number looks odd."""
    try:
        return normalize_phone(number, get_settings().default_region).e164
    except InvalidPhoneError:
        logger.warning("suppression_key_not_normalizable")  # never log the number itself
        return number


class SuppressionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # -- reads -----------------------------------------------------------------------------

    def is_suppressed(self, normalized_phone_number: str) -> bool:
        stmt = select(Suppression.id).where(
            Suppression.phone_number == canonical_number(normalized_phone_number)
        )
        return self.db.execute(stmt).first() is not None

    def suppressed_among(self, normalized_phone_numbers: Collection[str]) -> set[str]:
        """The subset of the given numbers that is suppressed: one query for a whole page
        instead of one per contact. Same match rule as is_suppressed; the returned strings
        are the CALLER's spellings, so a membership test on them keeps working."""
        if not normalized_phone_numbers:
            return set()
        by_key: dict[str, list[str]] = {}
        for number in normalized_phone_numbers:
            by_key.setdefault(canonical_number(number), []).append(number)
        found = self.db.execute(
            select(Suppression.phone_number).where(Suppression.phone_number.in_(by_key))
        ).scalars()
        return {original for key in found for original in by_key[key]}

    def get_by_phone(self, normalized_phone_number: str) -> Suppression | None:
        return self.db.execute(
            select(Suppression).where(
                Suppression.phone_number == canonical_number(normalized_phone_number)
            )
        ).scalar_one_or_none()

    def get_by_id(self, suppression_id: uuid.UUID) -> Suppression | None:
        return self.db.get(Suppression, suppression_id)

    def list(
        self, *, phone: str | None, limit: int, offset: int
    ) -> tuple[Sequence[Suppression], int]:
        filters = []
        if phone is not None:
            filters.append(Suppression.phone_number == canonical_number(phone))
        total = self.db.execute(
            select(func.count()).select_from(Suppression).where(*filters)
        ).scalar_one()
        rows = (
            self.db.execute(
                select(Suppression)
                .where(*filters)
                .order_by(Suppression.requested_at.desc(), Suppression.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return rows, total

    # -- writes ----------------------------------------------------------------------------

    def add(self, suppression: Suppression) -> Suppression:
        """Plain insert (kept for the existing callers/tests). Prefer insert_if_absent."""
        suppression.phone_number = canonical_number(suppression.phone_number)
        self.db.add(suppression)
        self.db.flush()
        return suppression

    def insert_if_absent(
        self,
        phone_number: str,
        *,
        source: SuppressionSource,
        reason: str | None,
        contact_id: uuid.UUID | None = None,
        created_by: str | None = None,
    ) -> uuid.UUID | None:
        """Idempotent write. Returns the new row's id, or None when the number was already
        suppressed (or this contact already has a row) -- so only the call that actually
        inserted audits, and N concurrent writers produce exactly one audit trail.

        RETURNING, not rowcount: psycopg reports -1 for this statement, which is truthy."""
        inserted = self.db.execute(
            pg_insert(Suppression)
            .values(
                id=uuid.uuid4(),
                contact_id=contact_id,
                phone_number=canonical_number(phone_number),
                reason=reason,
                source=source,
                created_by=created_by,
            )
            .on_conflict_do_nothing()  # any unique violation: number OR contact already there
            .returning(Suppression.id)
        ).first()
        self.db.flush()
        return inserted[0] if inserted is not None else None

    def bulk_insert_if_absent(
        self,
        phone_numbers: Sequence[str],
        *,
        source: SuppressionSource,
        reason: str | None,
        created_by: str | None,
    ) -> int:
        """Insert many canonical numbers in one statement; returns how many were NEW.
        Callers pass already-normalized, de-duplicated numbers in bounded chunks."""
        if not phone_numbers:
            return 0
        rows = [
            {
                "id": uuid.uuid4(),
                "phone_number": number,
                "reason": reason,
                "source": source,
                "created_by": created_by,
            }
            for number in phone_numbers
        ]
        inserted = self.db.execute(
            pg_insert(Suppression)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[Suppression.phone_number])
            .returning(Suppression.id)
        ).all()
        self.db.flush()
        return len(inserted)

    def remove(self, suppression: Suppression) -> None:
        self.db.execute(delete(Suppression).where(Suppression.id == suppression.id))
        self.db.expire_all()
        self.db.flush()
