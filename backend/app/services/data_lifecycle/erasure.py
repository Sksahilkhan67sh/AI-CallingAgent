"""Erasure and post-restore re-application -- CP15A.

`erase()` removes one person's data across EVERY contact that shares their number (all
campaigns): contact PII scrubbed, transcript turns deleted, working-memory content cleared,
analysis free text cleared. The number is added to the global do-not-call list by default
(otherwise a re-import would call the person again) and a keyed fingerprint -- never the
number -- is appended to `erasure_log`. A legal hold on ANY covered record refuses the whole
request (nothing is changed). Idempotent: a repeat finds nothing left to scrub and succeeds.

`reapply_erasures()` re-enforces every `erasure_log` entry against (restored) data: a
restore from an older backup would otherwise bring erased people back.

Not covered (DOCUMENTED LIMITATIONS): copies held by Dograh, and copies inside backups that
pre-date the request (they age out; `reapply_erasures` re-scrubs them after a restore).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Text, cast, delete, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import ContactStatus, SuppressionSource
from app.models.erasure_log import ErasureLog
from app.models.working_memory_snapshot import WorkingMemorySnapshot
from app.repositories.suppression_repository import SuppressionRepository, canonical_number
from app.services.audit_service import record_audit_event
from app.services.data_lifecycle.fingerprint import PLACEHOLDER_PREFIX, erasure_fingerprint
from app.services.data_lifecycle.retention import scrub_contacts
from app.services.data_lifecycle.sql_util import rowcount
from app.services.phone import InvalidPhoneError, last4, normalize_phone

# Contact statuses that may keep their value after a scrub; anything still "live" becomes
# CLOSED so a scrubbed contact can never be dialed.
_TERMINAL = (ContactStatus.COMPLETED, ContactStatus.COMPLETED_PARTIAL, ContactStatus.CLOSED)


@dataclass
class ErasureResult:
    counts: dict[str, int] = field(default_factory=dict)
    fingerprint_prefix: str | None = None
    last4: str | None = None
    already_erased: bool = False
    suppressed: bool = False


def _scrub_content(db: Session, contact_ids: list[uuid.UUID]) -> dict[str, int]:
    """Delete/clear every content-bearing row of the given contacts. No hold check."""
    attempt_ids = select(CallAttempt.id).where(CallAttempt.contact_id.in_(contact_ids))
    session_ids = select(ConversationSession.id).where(
        ConversationSession.call_attempt_id.in_(attempt_ids)
    )
    messages = rowcount(db.execute(
        delete(ConversationMessage).where(ConversationMessage.session_id.in_(session_ids))
    ))
    snapshots = rowcount(db.execute(
        update(WorkingMemorySnapshot)
        .where(
            WorkingMemorySnapshot.attempt_id.in_(attempt_ids),
            or_(
                cast(WorkingMemorySnapshot.captured_entities, Text) != "{}",
                cast(WorkingMemorySnapshot.objections_raised, Text) != "[]",
                WorkingMemorySnapshot.last_agent_utterance.is_not(None),
            ),
        )
        .values(captured_entities={}, objections_raised=[], last_agent_utterance=None)
    ))
    analyses = rowcount(db.execute(
        update(CallAnalysis)
        .where(
            CallAnalysis.contact_id.in_(contact_ids),
            or_(
                CallAnalysis.summary.is_not(None),
                CallAnalysis.feedback.is_not(None),
                CallAnalysis.error_message.is_not(None),
                cast(CallAnalysis.key_facts, Text) != "[]",
                cast(CallAnalysis.objections, Text) != "[]",
                cast(CallAnalysis.customer_needs, Text) != "[]",
            ),
        )
        .values(
            summary=None,
            feedback=None,
            error_message=None,
            key_facts=[],
            objections=[],
            customer_needs=[],
        )
    ))
    return {"messages": messages, "snapshots": snapshots, "analyses": analyses}


def _scrub_contact_rows(db: Session, contact_ids: list[uuid.UUID], now: datetime) -> int:
    n = scrub_contacts(db, contact_ids, now)
    db.execute(
        update(Contact)
        .where(Contact.id.in_(contact_ids), Contact.status.not_in(_TERMINAL))
        .values(status=ContactStatus.CLOSED)
    )
    return n


def _held(db: Session, contact_ids: list[uuid.UUID]) -> int:
    rows = db.execute(
        select(Contact.id)
        .join(Campaign, Campaign.id == Contact.campaign_id)
        .where(
            Contact.id.in_(contact_ids),
            or_(Contact.legal_hold.is_(True), Campaign.legal_hold.is_(True)),
        )
    ).all()
    return len(rows)


def erase(
    db: Session,
    *,
    actor: str,
    now: datetime,
    phone_number: str | None = None,
    contact_id: uuid.UUID | None = None,
    reason: str | None = None,
    suppress: bool = True,
) -> ErasureResult:
    if (phone_number is None) == (contact_id is None):
        raise ValidationError("Provide exactly one of phone_number or contact_id")

    if contact_id is not None:
        contact = db.get(Contact, contact_id, populate_existing=True)
        if contact is None:
            raise NotFoundError(f"Contact {contact_id} not found")
        if contact.purged_at is not None or contact.normalized_phone_number.startswith(
            PLACEHOLDER_PREFIX
        ):
            return ErasureResult(already_erased=True, counts={"contacts": 0})
        e164 = canonical_number(contact.normalized_phone_number)
    else:
        assert phone_number is not None
        try:
            e164 = normalize_phone(phone_number, get_settings().default_region).e164
        except InvalidPhoneError as exc:
            raise ValidationError(f"Invalid phone number ({exc.code})") from None

    contacts = list(
        db.execute(
            select(Contact.id)
            .where(Contact.normalized_phone_number == e164)
            .with_for_update()
        ).scalars()
    )
    if contacts and (held := _held(db, contacts)):
        raise ConflictError(
            f"Erasure refused: {held} covered record(s) are under legal hold. "
            "Clear the hold deliberately, then retry. Nothing was changed."
        )

    counts: dict[str, int] = {"contacts": 0, "messages": 0, "snapshots": 0, "analyses": 0}
    if contacts:
        counts.update(_scrub_content(db, contacts))
        counts["contacts"] = _scrub_contact_rows(db, contacts, now)

    suppressed = False
    if suppress:
        new_id = SuppressionRepository(db).insert_if_absent(
            e164,
            source=SuppressionSource.MANUAL_API,
            reason="erasure_request",
            created_by=actor,
        )
        suppressed = new_id is not None
    counts["suppression_added"] = int(suppressed)

    fingerprint = erasure_fingerprint(e164)
    log = ErasureLog(
        id=uuid.uuid4(), fingerprint=fingerprint, actor=actor, reason=reason, counts=counts
    )
    db.add(log)
    db.flush()
    record_audit_event(
        db,
        actor=actor,
        action="data.erasure",
        entity_type="erasure_log",
        entity_id=log.id,
        # Fingerprint prefix + last four digits only -- never the number.
        metadata={"fingerprint": fingerprint[:16], "last4": last4(e164), "counts": counts},
    )
    return ErasureResult(
        counts=counts,
        fingerprint_prefix=fingerprint[:16],
        last4=last4(e164),
        suppressed=suppressed,
    )


# -- re-application after a restore --------------------------------------------------------------


@dataclass
class ReapplyReport:
    fingerprints: int = 0
    contacts_scanned: int = 0
    contacts_matched: int = 0
    contacts_scrubbed: int = 0
    skipped_legal_hold: int = 0
    erased_missing_from_dnc: int = 0
    applied: bool = False


def _contact_pages(db: Session, page: int) -> Iterator[list[tuple[uuid.UUID, str, uuid.UUID]]]:
    last: uuid.UUID | None = None
    while True:
        stmt = (
            select(Contact.id, Contact.normalized_phone_number, Contact.campaign_id)
            .where(Contact.purged_at.is_(None))
            .order_by(Contact.id)
            .limit(page)
        )
        if last is not None:
            stmt = stmt.where(Contact.id > last)
        rows = [(r[0], r[1], r[2]) for r in db.execute(stmt).all()]
        if not rows:
            return
        last = rows[-1][0]
        yield rows


def reapply_erasures(
    db: Session, *, now: datetime, apply: bool = False, page_size: int = 1000
) -> ReapplyReport:
    """Re-scrub every contact whose number matches an `erasure_log` fingerprint. Dry-run by
    default. Records under legal hold are SKIPPED and counted for a human decision (a hold set
    on restored data must not be overridden silently). The erased numbers themselves are not
    stored, so a DNC entry lost in a restore cannot be recreated here: the report counts how
    many erased fingerprints have no matching suppression row, to be reconciled from the
    secondary DNC copy (CP15B)."""
    fingerprints = set(db.execute(select(ErasureLog.fingerprint).distinct()).scalars())
    report = ReapplyReport(fingerprints=len(fingerprints), applied=apply)
    if not fingerprints:
        return report

    for rows in _contact_pages(db, page_size):
        matched: list[uuid.UUID] = []
        for contact_id, number, _campaign in rows:
            report.contacts_scanned += 1
            if erasure_fingerprint(canonical_number(number)) in fingerprints:
                matched.append(contact_id)
        if not matched:
            continue
        report.contacts_matched += len(matched)
        free = [c for c in matched if _held(db, [c]) == 0]
        report.skipped_legal_hold += len(matched) - len(free)
        if apply and free:
            _scrub_content(db, free)
            report.contacts_scrubbed += _scrub_contact_rows(db, free, now)
            db.flush()
        elif free:
            report.contacts_scrubbed += len(free)  # would be scrubbed (dry run)

    from app.models.suppression import Suppression  # local: read-only reconciliation count

    in_dnc = {
        erasure_fingerprint(canonical_number(n))
        for n in db.execute(select(Suppression.phone_number)).scalars()
    }
    report.erased_missing_from_dnc = len(fingerprints - in_dnc)
    return report
