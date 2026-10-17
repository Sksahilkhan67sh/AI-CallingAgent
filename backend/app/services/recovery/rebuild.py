"""Rebuild the Redis retry schedule from PostgreSQL (P1-2).

`contact.next_retry_at` / `retry_attempt_id` / `retry_attempt_number` are the source of truth for
a scheduled retry; the `recovery:scheduled` ZSET is only a cache of them. If Redis is flushed,
restarted without persistence or fails over to a stale replica, this puts every missing entry
back. It is safe to run at any time, from any number of workers:

* the ZSET member is deterministic (see RecoveryJob), so the same retry is always the same member;
* entries are added with ZADD NX -- an entry that already exists is left alone, which also keeps
  its current score (a dispatcher re-check that pushed it later is not undone);
* each page is read FOR UPDATE SKIP LOCKED, so concurrent rebuilders split the rows between them.

Contacts that are not RETRY_SCHEDULED are ignored: a retry the dialer already claimed is done.
Dispatching a rebuilt entry can never dial twice: the dispatcher enqueues with the per-attempt
guard and the dialer holds UNIQUE(contact, attempt_number).
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.enums import ContactStatus
from app.services.recovery.job import RecoveryJob
from app.services.recovery.scheduler import RecoveryScheduler

logger = logging.getLogger("recovery.rebuild")


def rebuild_retry_schedule(
    session_factory: Callable[[], Session],
    scheduler: RecoveryScheduler,
    *,
    batch_size: int | None = None,
) -> int:
    """Returns how many entries were missing from Redis and have been added."""
    size = batch_size or get_settings().retry_rebuild_batch_size
    added = 0
    last_id: uuid.UUID | None = None
    while True:
        with session_factory() as db:
            stmt = (
                select(Contact)
                .where(
                    Contact.status == ContactStatus.RETRY_SCHEDULED,
                    Contact.next_retry_at.is_not(None),
                )
                .order_by(Contact.id)
                .limit(size)
                .with_for_update(skip_locked=True)
            )
            if last_id is not None:
                stmt = stmt.where(Contact.id > last_id)
            contacts = list(db.execute(stmt).scalars())
            if not contacts:
                return added
            entries: dict[str, float] = {}
            for contact in contacts:
                job = _job_for(db, contact)
                if job is None:
                    logger.error(
                        "retry_rebuild_unrecoverable",
                        extra={"contact_id": str(contact.id)},
                    )
                    continue
                assert contact.next_retry_at is not None
                entries[job.to_json()] = contact.next_retry_at.astimezone(UTC).timestamp()
            added += scheduler.add_missing(entries)
            last_id = contacts[-1].id
            db.rollback()  # read-only: release the row locks
            if len(contacts) < size:
                return added


def _job_for(db: Session, contact: Contact) -> RecoveryJob | None:
    attempt_id = contact.retry_attempt_id
    number = contact.retry_attempt_number
    if attempt_id is None or number is None:
        # Not written by P1-2 code and not backfilled: derive from the most recent attempt.
        latest = db.execute(
            select(CallAttempt)
            .where(CallAttempt.contact_id == contact.id)
            .order_by(CallAttempt.attempt_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return None
        attempt_id, number = latest.id, latest.attempt_number + 1
    return RecoveryJob.new(
        attempt_id=attempt_id,
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        attempt_number=number,
    )
