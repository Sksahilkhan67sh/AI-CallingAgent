"""Re-apply every erasure_log entry after a database restore (CP15A).

    python -m app.scripts.reapply_erasures            # DRY RUN
    python -m app.scripts.reapply_erasures --apply

A restore from an older backup brings erased people back. This scans contacts, recognises
each erased number by its keyed fingerprint (the number itself is not stored anywhere) and
scrubs it again. Records under legal hold are skipped and counted. It also reports how many
erased fingerprints have no do-not-call row (reconcile from the secondary DNC copy).
Run it BEFORE releasing the kill switch (docs/runbooks/BACKUP-AND-RESTORE.md, CP15B).
"""

import argparse
import sys
from datetime import UTC, datetime

from app.core.database import SessionLocal
from app.services.data_lifecycle.erasure import reapply_erasures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        report = reapply_erasures(db, now=datetime.now(UTC), apply=args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"reapply_erasures {mode}: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
