"""CallAttempt persistence."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt


class CallAttemptRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, attempt_id: uuid.UUID) -> CallAttempt | None:
        return self.db.get(CallAttempt, attempt_id)

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
