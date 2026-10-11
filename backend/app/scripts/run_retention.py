"""Run the data-retention engine (CP15A).

    python -m app.scripts.run_retention            # DRY RUN: counts only, no data changes
    python -m app.scripts.run_retention --apply    # delete / scrub rows past retention

Schedule it externally (cron or a Kubernetes CronJob), e.g. daily off-peak:
    17 3 * * *  cd /app && python -m app.scripts.run_retention --apply
There is deliberately no long-running scheduler. Two runs can never overlap (Postgres
advisory lock); a second one exits with code 3. Each run writes one `retention_run` row and
one audit event, both with COUNTS ONLY.

Exit codes: 0 completed, 2 partial (runtime budget reached -- run again), 3 lock held,
1 failed. The suppression (do-not-call) table is never touched.
"""

import argparse
import sys
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.services.data_lifecycle.retention import (
    RetentionConfig,
    RetentionLockHeld,
    run_retention,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="make changes (default: dry run)")
    args = parser.parse_args(argv)

    config = RetentionConfig.from_settings(get_settings())
    with SessionLocal() as db:
        try:
            result = run_retention(
                db,
                lock_engine=engine,
                config=config,
                now=datetime.now(UTC),
                apply=args.apply,
            )
        except RetentionLockHeld:
            print("retention: another run holds the lock; exiting", file=sys.stderr)
            return 3
    print(f"retention {result.mode}: {result.status}")
    for key, value in result.counts.items():
        print(f"  {key}: {value}")
    if result.disabled:
        print(f"  disabled (keep forever): {', '.join(result.disabled)}")
    return {"completed": 0, "partial": 2}.get(result.status, 1)


if __name__ == "__main__":
    sys.exit(main())
