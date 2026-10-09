"""One outbound gate: the CP11 kill switch plus the CP14 daily budget.

Everything that decides "may a NEW outbound call be placed at all right now" lives here, so
the three places that ask (before reading the stream, before admission, and just before the
durable attempt claim) cannot drift apart. A non-None answer means: do not dial, do not ack,
do not touch retry state -- leave the job where it is.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.services import kill_switch, spend_cap


def block_reason(db: Session, now: datetime | None = None) -> str | None:
    # Kill switch first: it needs no database, and when it is on the budget is irrelevant.
    return kill_switch.block_reason() or spend_cap.block_reason(db, now)


def is_budget_reason(reason: str) -> bool:
    return reason.startswith("budget_")
