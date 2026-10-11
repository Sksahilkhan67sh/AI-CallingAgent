"""Erase one person's data (CP15A) -- CLI equivalent of POST /api/v1/admin/data/erasure.

    python -m app.scripts.erase_data --phone "+14155550100" --reason "subject request"
    python -m app.scripts.erase_data --contact-id <uuid> --reason "subject request" --no-suppress

Applies to ALL contacts sharing the number across campaigns, adds the number to the do-not-call
list unless --no-suppress, appends an erasure_log fingerprint. A legal hold on any covered
record refuses the request (exit 2). The number is never printed back.
"""

import argparse
import sys
import uuid
from datetime import UTC, datetime

from app.core.database import SessionLocal
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.services.data_lifecycle.erasure import erase


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--phone")
    target.add_argument("--contact-id", type=uuid.UUID)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--no-suppress", action="store_true")
    parser.add_argument("--actor", default="cli")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        try:
            result = erase(
                db,
                actor=f"cli:{args.actor}",
                now=datetime.now(UTC),
                phone_number=args.phone,
                contact_id=args.contact_id,
                reason=args.reason,
                suppress=not args.no_suppress,
            )
            db.commit()
        except ConflictError as exc:
            db.rollback()
            print(exc.message, file=sys.stderr)
            return 2
        except (ValidationError, NotFoundError) as exc:
            db.rollback()
            print(exc.message, file=sys.stderr)
            return 1
    print(f"erased (last4={result.last4}): {result.counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
