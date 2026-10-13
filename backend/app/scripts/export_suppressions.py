"""Export the global do-not-call list to an encrypted secondary copy (CP15B).

    BACKUP_AGE_RECIPIENT=age1... DNC_DEST=/mnt/dnc python -m app.scripts.export_suppressions

Writes DNC_DEST/dnc-<UTC>.csv.age (+ .manifest.json with row count, newest created_at and the
SHA-256 of the encrypted file). The copy holds normalized number, source, reason and
created_at only. `--kind erasure` writes erasure-<UTC>.csv.age with the erasure_log
FINGERPRINTS (never numbers): that log lives in the database being restored, so without this
copy an erasure made after the restore point could not be re-applied.

Run it every ~15 minutes from cron / a Kubernetes CronJob, to storage that is
NOT the database backup storage (see docs/runbooks/BACKUP-AND-RESTORE.md). Encryption is
mandatory; the host holds the PUBLIC key only. Lifecycle (expiring old copies) belongs to the
storage policy: the writer needs no delete permission.
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.database import SessionLocal
from app.services.dnc_copy import DncCopyError, export_copy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dest", default=os.environ.get("DNC_DEST"))
    parser.add_argument("--kind", choices=("dnc", "erasure"), default="dnc")
    parser.add_argument("--now", help="ISO-8601 UTC time (tests / drills)")
    args = parser.parse_args(argv)

    recipients = [r for r in os.environ.get("BACKUP_AGE_RECIPIENT", "").split(",") if r]
    if not args.dest:
        print("DNC_DEST (or --dest) is required", file=sys.stderr)
        return 1
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    try:
        with SessionLocal() as db:
            manifest = export_copy(
                db,
                Path(args.dest),
                recipients=recipients,
                now=now,
                env=os.environ.get("BACKUP_ENV", "production"),
                allow_unencrypted=os.environ.get("BACKUP_ALLOW_UNENCRYPTED") is not None,
                kind=args.kind,
            )
    except DncCopyError as exc:
        print(f"export_suppressions: {exc}", file=sys.stderr)
        return 1
    print(
        f"SUMMARY script=export_suppressions status=ok artifact={manifest['artifact']} "
        f"rows={manifest['row_count']} sha256={manifest['sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
