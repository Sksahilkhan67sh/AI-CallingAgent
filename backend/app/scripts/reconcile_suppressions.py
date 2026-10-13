"""Re-add do-not-call numbers (or erasure fingerprints) lost in a restore (CP15B).

    python -m app.scripts.reconcile_suppressions --copy dnc-<UTC>.csv.age --identity KEY   # DRY RUN
    python -m app.scripts.reconcile_suppressions --copy ... --identity KEY --apply

ONLY ADDS: it never deletes or modifies an existing row. For each number in the verified copy
that is missing from the database it inserts it (original source kept, reason
'restore_reconcile'), unless the restored database holds a CP14 `suppression.removed` audit
event for that number that is NEWER than the copy entry (a deliberate admin removal inside the
restored data). A removal that happened AFTER the restore point is not in the restored audit
log and cannot be seen: such a number is re-added, which is the safe direction (an opt-out
must never be missed).

`--kind erasure` does the same for the erasure_log: it appends the fingerprints that the
restored database lacks (append-only; nothing is changed or deleted), after which
`python -m app.scripts.reapply_erasures` can scrub the people who came back from the backup.
Run the DNC reconcile FIRST, then the erasure reconcile, then reapply_erasures.

Prints counts only, never numbers or fingerprints. Apply writes one counts-only audit
event. Needs the PRIVATE age key (--identity FILE), so run it on the restore host.
"""

import argparse
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.enums import SuppressionSource
from app.models.erasure_log import ErasureLog
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository, canonical_number
from app.services.audit_service import record_audit_event
from app.services.dnc_copy import CopyRow, DncCopyError, ErasureCopyRow, read_copy
from app.services.phone import phone_fingerprint

ACTOR = "restore_reconcile"


def _removal_times(db: Session) -> dict[str, datetime]:
    """fingerprint -> newest suppression.removed time present in this database."""
    fp = AuditLog.event_metadata["fingerprint"].astext
    rows = db.execute(
        select(fp, func.max(AuditLog.created_at))
        .where(AuditLog.action == "suppression.removed", fp.is_not(None))
        .group_by(fp)
    )
    return {f: t for f, t in rows}


def reconcile(db: Session, rows: Iterable[CopyRow], *, apply: bool) -> dict[str, int]:
    repo = SuppressionRepository(db)
    key = get_settings().jwt_signing_key
    removed = _removal_times(db)
    counts = dict.fromkeys(
        ("copy_rows", "already_present", "skipped_removed", "would_add", "added"), 0
    )
    for row in rows:
        counts["copy_rows"] += 1
        e164 = canonical_number(row.phone_number)
        if db.execute(select(Suppression.id).where(Suppression.phone_number == e164)).first():
            counts["already_present"] += 1
            continue
        removed_at = removed.get(phone_fingerprint(e164, key))
        if removed_at is not None and removed_at > row.created_at:
            counts["skipped_removed"] += 1
            continue
        counts["would_add"] += 1
        if apply:
            try:
                source = SuppressionSource(row.source)
            except ValueError:
                source = SuppressionSource.MANUAL_API
            if repo.insert_if_absent(
                e164, source=source, reason="restore_reconcile", created_by=ACTOR
            ):
                counts["added"] += 1
    if apply:
        record_audit_event(
            db,
            actor=ACTOR,
            action="suppression.reconciled",
            entity_type="suppression",
            entity_id=None,
            metadata=dict(counts),
        )
    return dict(counts)


def reconcile_erasures(
    db: Session, rows: Iterable[ErasureCopyRow], *, apply: bool
) -> dict[str, int]:
    counts = dict.fromkeys(("copy_rows", "already_present", "would_add", "added"), 0)
    known = set(db.execute(select(ErasureLog.fingerprint)).scalars())
    for row in rows:
        counts["copy_rows"] += 1
        if row.fingerprint in known:
            counts["already_present"] += 1
            continue
        counts["would_add"] += 1
        if apply:
            db.add(
                ErasureLog(
                    fingerprint=row.fingerprint,
                    requested_at=row.created_at,
                    actor=ACTOR,
                    reason="restored from the secondary erasure copy",
                    counts={},
                )
            )
            known.add(row.fingerprint)
            counts["added"] += 1
    if apply:
        db.flush()
        record_audit_event(
            db,
            actor=ACTOR,
            action="erasure_log.reconciled",
            entity_type="erasure_log",
            entity_id=None,
            metadata=dict(counts),
        )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--copy", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--kind", choices=("dnc", "erasure"), default="dnc")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest, rows = read_copy(args.copy, args.identity, args.kind)
    except DncCopyError as exc:
        print(f"reconcile_suppressions: {exc}", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        table = Suppression if args.kind == "dnc" else ErasureLog
        stamp = Suppression.requested_at if args.kind == "dnc" else ErasureLog.requested_at
        db_rows = db.execute(select(func.count()).select_from(table)).scalar_one()
        db_newest = db.execute(select(func.max(stamp))).scalar_one()
        counts = (
            reconcile(db, list(rows), apply=args.apply)
            if args.kind == "dnc"
            else reconcile_erasures(db, list(rows), apply=args.apply)
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(
        f"reconcile_suppressions[{args.kind}] {mode}: {counts} | db_rows={db_rows} "
        f"db_newest={db_newest.astimezone(UTC).isoformat() if db_newest else None} "
        f"copy_newest={manifest['newest_created_utc']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
