"""Read-only report to run right after a restore (CP15B). It NEVER modifies data.

    python -m app.scripts.post_restore_report [--json] [--limit 20]

Shows how much was lost (newest timestamp per key table) and which work was stranded
mid-flight: contacts left DIALING / RETRY_SCHEDULED, call attempts left INITIATED, analyses left
PROCESSING -- bucketed by age, ids and statuses only (no phone numbers). The call-attempt
sweeper (CP12D) does not exist yet, so these do NOT repair themselves: a human decides what to do
with each bucket. Also prints two short fingerprints (erasure pepper, JWT signing key) so the
operator can confirm the SAME secrets are in place as at deploy time -- without them
`reapply_erasures` / the removal-aware DNC reconcile silently match nothing.

The transaction is READ ONLY at the database level, so even a bug here cannot write.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.conversation import ConversationMessage
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.models.erasure_log import ErasureLog
from app.models.processed_event import ProcessedEvent
from app.models.retention_run import RetentionRun
from app.models.suppression import Suppression
from app.services.data_lifecycle.fingerprint import erasure_fingerprint
from app.services.phone import phone_fingerprint

_BUCKETS = (
    ("under_1h", timedelta(hours=1)),
    ("1h_to_24h", timedelta(hours=24)),
    ("1d_to_7d", timedelta(days=7)),
    ("over_7d", None),
)
_NEWEST = {
    "audit_log.created_at": AuditLog.created_at,
    "call_attempt.started_at": CallAttempt.started_at,
    "call_analysis.updated_at": CallAnalysis.updated_at,
    "contact.updated_at": Contact.updated_at,
    "conversation_message.created_at": ConversationMessage.created_at,
    "erasure_log.requested_at": ErasureLog.requested_at,
    "processed_event.received_at": ProcessedEvent.received_at,
    "retention_run.started_at": RetentionRun.started_at,
    "suppression.requested_at": Suppression.requested_at,
}


def _bucket(age: timedelta) -> str:
    for name, limit in _BUCKETS:
        if limit is None or age < limit:
            return name
    return "over_7d"  # pragma: no cover


def _stranded(db: Session, stmt: Any, now: datetime, limit: int) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {n: {"count": 0, "sample": []} for n, _ in _BUCKETS}
    total = 0
    for row_id, status, stamp in db.execute(stmt):
        b = buckets[_bucket(now - stamp) if stamp else "over_7d"]
        b["count"] += 1
        total += 1
        if len(b["sample"]) < limit:
            b["sample"].append({"id": str(row_id), "status": getattr(status, "value", str(status))})
    return {"total": total, "by_age": buckets}


def build_report(db: Session, now: datetime, limit: int = 20) -> dict[str, Any]:
    db.execute(text("SET TRANSACTION READ ONLY"))
    settings = get_settings()
    return {
        "generated_utc": now.isoformat(),
        "newest": {
            name: (lambda v: v.isoformat() if v else None)(
                db.execute(select(func.max(col))).scalar_one()
            )
            for name, col in _NEWEST.items()
        },
        "stranded_contacts": _stranded(
            db,
            select(Contact.id, Contact.status, Contact.updated_at).where(
                Contact.status.in_([ContactStatus.DIALING, ContactStatus.RETRY_SCHEDULED])
            ),
            now,
            limit,
        ),
        "stranded_call_attempts": _stranded(
            db,
            select(CallAttempt.id, CallAttempt.state, CallAttempt.started_at).where(
                CallAttempt.state == CallAttemptState.INITIATED
            ),
            now,
            limit,
        ),
        "stranded_analyses": _stranded(
            db,
            select(CallAnalysis.id, CallAnalysis.status, CallAnalysis.updated_at).where(
                CallAnalysis.status == AnalysisStatus.PROCESSING
            ),
            now,
            limit,
        ),
        "erasure_log_rows": db.execute(select(func.count()).select_from(ErasureLog)).scalar_one(),
        "secret_checks": {
            "erasure_pepper": erasure_fingerprint("pepper-check:v1", settings)[:12],
            "jwt_signing_key": phone_fingerprint("jwt-check:v1", settings.jwt_signing_key)[:12],
        },
    }


def _render(report: dict[str, Any]) -> str:
    lines = [
        f"Post-restore report ({report['generated_utc']}) -- READ ONLY",
        "",
        "Newest data per table:",
    ]
    lines += [f"  {k}: {v}" for k, v in report["newest"].items()]
    for key, title in (
        ("stranded_contacts", "Contacts in DIALING / RETRY_SCHEDULED"),
        ("stranded_call_attempts", "Call attempts in INITIATED"),
        ("stranded_analyses", "Analyses in PROCESSING"),
    ):
        block = report[key]
        lines += ["", f"{title}: {block['total']}"]
        for name, data in block["by_age"].items():
            lines.append(f"  {name}: {data['count']}")
            lines += [f"    {s['id']} {s['status']}" for s in data["sample"]]
    lines += [
        "",
        f"erasure_log rows: {report['erasure_log_rows']}",
        f"secret checks (compare with the values recorded at deploy): {report['secret_checks']}",
        "",
        "Nothing was changed. Decide per bucket whether to re-enqueue or close; see the runbook.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        try:
            report = build_report(db, datetime.now(UTC), args.limit)
        finally:
            db.rollback()
    print(json.dumps(report, indent=2) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
