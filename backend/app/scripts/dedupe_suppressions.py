"""Resolve duplicate phone numbers in `suppression` BEFORE the CP14 migration.

    python -m app.scripts.dedupe_suppressions            # DRY RUN
    python -m app.scripts.dedupe_suppressions --apply    # deletes the later duplicates

The CP14 migration adds a global UNIQUE constraint on suppression.phone_number and refuses to
run while duplicates exist (choosing which row survives is a data decision, not a schema
one). For each number this keeps the EARLIEST row (by requested_at, then contact_id) and
deletes the rest -- the number stays suppressed through the kept row. Works on the
pre-migration schema (it only uses contact_id, phone_number, requested_at). Ids and masked
numbers only in the output. Idempotent.
"""

import argparse
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.services.phone import mask_phone_number

_DUPLICATES_SQL = text(
    """
    SELECT contact_id, phone_number, requested_at, rn FROM (
      SELECT contact_id, phone_number, requested_at,
             row_number() OVER (
               PARTITION BY phone_number ORDER BY requested_at, contact_id
             ) AS rn
      FROM suppression
    ) ranked
    WHERE phone_number IN (
      SELECT phone_number FROM suppression GROUP BY phone_number HAVING count(*) > 1
    )
    ORDER BY phone_number, rn
    """
)


def run(db: Session, *, apply: bool) -> dict:
    rows = db.execute(_DUPLICATES_SQL).all()
    extras = [r for r in rows if r.rn > 1]
    kept = {r.phone_number for r in rows if r.rn == 1}
    if apply and extras:
        db.execute(
            text("DELETE FROM suppression WHERE contact_id = ANY(:ids)"),
            {"ids": [r.contact_id for r in extras]},
        )
        db.commit()
    else:
        db.rollback()
    return {
        "applied": apply,
        "numbers_with_duplicates": len(kept),
        "rows_to_delete" if not apply else "rows_deleted": len(extras),
        "detail": [
            f"delete contact_id={r.contact_id} number={mask_phone_number(r.phone_number)} "
            f"requested_at={r.requested_at.isoformat()}"
            for r in extras[:50]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete duplicates (default: dry run)")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        result = run(db, apply=args.apply)
    mode = "APPLIED" if result.pop("applied") else "DRY RUN (nothing written; use --apply)"
    detail = result.pop("detail")
    print(f"dedupe_suppressions: {mode}")
    for key, value in result.items():
        print(f"  {key}: {value}")
    for line in detail:
        print(f"    - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
