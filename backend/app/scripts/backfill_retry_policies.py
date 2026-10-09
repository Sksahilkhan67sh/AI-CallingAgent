"""Create the default RetryPolicy row for every campaign that has none.

    python -m app.scripts.backfill_retry_policies            # DRY RUN
    python -m app.scripts.backfill_retry_policies --apply

Not required for safety -- a campaign without a row is already judged by the default policy
(`get_effective_policy`) -- but it makes the stored state match what is in force, so the
admin API and the dashboard show a real row. Idempotent and race-safe.
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.campaign import Campaign
from app.models.retry_policy import RetryPolicy
from app.services.retry_policy_service import ensure_policy


def run(db: Session, *, apply: bool) -> dict:
    missing = list(
        db.execute(
            select(Campaign.id)
            .outerjoin(RetryPolicy, RetryPolicy.campaign_id == Campaign.id)
            .where(RetryPolicy.id.is_(None))
            .order_by(Campaign.id)
        ).scalars()
    )
    created = 0
    if apply:
        created = sum(1 for campaign_id in missing if ensure_policy(db, campaign_id))
        db.commit()
    return {"applied": apply, "campaigns_without_policy": len(missing), "created": created}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="create the rows (default: dry run)")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        result = run(db, apply=args.apply)
    mode = "APPLIED" if result.pop("applied") else "DRY RUN (nothing written; use --apply)"
    print(f"backfill_retry_policies: {mode}")
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
