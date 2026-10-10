"""CP14B daily ESTIMATED-spend ledger for post-call analysis.

Deliberately separate from CP14's outbound-dialing cap (app/services/spend_cap.py): different
unit (analyses, not call minutes), different table, different settings.

What this does and does NOT guarantee (see docs/CHECKPOINT-14B-NOTES.md):

  * Reservations live in PostgreSQL (`analysis_budget_day`) and move through ONE atomic
    conditional upsert, so any number of concurrent workers cannot reserve past the cap.
    Redis is not involved in correctness.
  * Dograh runs the QA LLM itself, automatically, when a call ends -- BEFORE this backend can
    observe the run. This ledger therefore gates how many analyses this backend will
    PROCESS per day; it cannot stop Dograh from incurring QA cost. It is NOT a hard
    pre-spend cap. The enforceable lever is Dograh's QA node (sample rate, minimum duration,
    enabled flag), configured manually in Dograh.
  * Fail closed: with no cap configured the work is deferred (stays pending), never run
    unlimited. The mock provider has no cost and is not gated.
"""

import enum
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.call_analysis import CallAnalysis


class BudgetDecision(str, enum.Enum):
    RESERVED = "reserved"
    NOT_REQUIRED = "not_required"
    NOT_CONFIGURED = "budget_not_configured"
    CAP_REACHED = "budget_cap_reached"


def budget_required() -> bool:
    return get_settings().analysis_llm_provider != "mock"


def budget_day_for(now: datetime) -> date:
    return now.astimezone(ZoneInfo(get_settings().budget_timezone)).date()


def reserve(db: Session, analysis: CallAnalysis, now: datetime) -> BudgetDecision:
    """Atomically reserve this analysis's estimated cost against today's cap. Idempotent: an
    analysis that already holds a reservation never reserves again (retries are free)."""
    if not budget_required():
        return BudgetDecision.NOT_REQUIRED
    if analysis.reserved_cost is not None:
        return BudgetDecision.RESERVED
    settings = get_settings()
    cap = settings.analysis_daily_estimated_spend_cap
    estimate = settings.analysis_estimated_cost_per_analysis
    if cap is None or estimate is None:
        return BudgetDecision.NOT_CONFIGURED
    cost = Decimal(str(estimate))
    if cost > Decimal(str(cap)):
        # The first INSERT of a day is unconditional in SQL, so an estimate larger than the
        # whole cap must be refused up front (a cap of 0 blocks everything).
        return BudgetDecision.CAP_REACHED
    day = budget_day_for(now)
    row = db.execute(
        text(
            "INSERT INTO analysis_budget_day AS b (day, reserved_cost) VALUES (:day, :cost) "
            "ON CONFLICT (day) DO UPDATE SET reserved_cost = b.reserved_cost + :cost, "
            "updated_at = now() WHERE b.reserved_cost + :cost <= :cap "
            "RETURNING reserved_cost"
        ),
        {"day": day, "cost": cost, "cap": Decimal(str(cap))},
    ).first()
    if row is None:
        return BudgetDecision.CAP_REACHED
    analysis.budget_day = day
    analysis.reserved_cost = cost
    db.flush()
    return BudgetDecision.RESERVED


def release(db: Session, budget_day: date | None, reserved_cost: Decimal | None) -> None:
    """Return a reservation whose analysis provably did not consume QA (skipped before it
    ran). Never drives the ledger negative."""
    if budget_day is None or not reserved_cost or reserved_cost <= 0:
        return
    db.execute(
        text(
            "UPDATE analysis_budget_day SET reserved_cost = GREATEST(reserved_cost - :cost, 0), "
            "updated_at = now() WHERE day = :day"
        ),
        {"day": budget_day, "cost": reserved_cost},
    )
