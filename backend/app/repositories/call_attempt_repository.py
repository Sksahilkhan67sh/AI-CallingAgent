"""CallAttempt persistence."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.enums import CallAttemptState


class CallAttemptRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, attempt_id: uuid.UUID) -> CallAttempt | None:
        return self.db.get(CallAttempt, attempt_id)

    def get_by_contact_and_number(
        self, contact_id: uuid.UUID, attempt_number: int
    ) -> CallAttempt | None:
        stmt = select(CallAttempt).where(
            CallAttempt.contact_id == contact_id,
            CallAttempt.attempt_number == attempt_number,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_provider_call_id(self, provider_call_id: str) -> CallAttempt | None:
        stmt = select(CallAttempt).where(CallAttempt.provider_call_id == provider_call_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_for_contact(self, contact_id: uuid.UUID) -> list[CallAttempt]:
        stmt = (
            select(CallAttempt)
            .where(CallAttempt.contact_id == contact_id)
            .order_by(CallAttempt.attempt_number)
        )
        return list(self.db.execute(stmt).scalars())

    def add(self, attempt: CallAttempt) -> CallAttempt:
        self.db.add(attempt)
        self.db.flush()
        return attempt

    def get_or_create(self, contact_id: uuid.UUID, attempt_number: int) -> tuple[CallAttempt, bool]:
        """Checkpoint 03 Step 15/23: the safe claim pattern for "N
        workers race to process the same (contact_id, attempt_number)."
        Tries an INSERT; if the unique constraint rejects it (another
        worker/delivery already created the row), falls back to
        SELECT-ing the existing row. The database constraint -- not
        this method's ordering -- is what actually guarantees only one
        row ever exists; this just makes both outcomes usable by the
        caller. Returns (attempt, created)."""
        existing = self.get_by_contact_and_number(contact_id, attempt_number)
        if existing is not None:
            return existing, False

        attempt = CallAttempt(
            contact_id=contact_id,
            attempt_number=attempt_number,
            state=CallAttemptState.INITIATED,
        )
        try:
            with self.db.begin_nested():  # SAVEPOINT -- only this insert unwinds on conflict
                self.db.add(attempt)
                self.db.flush()
            return attempt, True
        except IntegrityError:
            existing = self.get_by_contact_and_number(contact_id, attempt_number)
            if existing is None:
                raise  # something else caused the conflict -- don't hide it
            return existing, False
