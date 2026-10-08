"""Daily dial / estimated-spend cap (CP14).

A BASIC safety limit, not billing. Every figure is an ESTIMATE; the provider-side limit in
Dograh remains the real backstop. Per-campaign budgets and monthly caps are CP18.

* The count comes from PostgreSQL (call attempts whose `started_at` falls inside today's
  budget window), never from Redis, which is not durable here: a Redis flush must not hand
  back a spent budget.
* Estimated spend = sum of stored call durations (attempts with no recorded duration count
  ESTIMATED_MINUTES_PER_UNKNOWN_ATTEMPT) x ESTIMATED_COST_PER_MINUTE.
* The day is BUDGET_TIMEZONE's calendar day, so the cap resumes by itself at local midnight.
* FAIL CLOSED: if the budget cannot be computed, nothing is dialed.
* The result is cached in-process for BUDGET_CHECK_CACHE_TTL_SECONDS. That bounds the work
  but means the cap is NOT exact: calls can overshoot by at most the dials STARTED within
  one TTL window (<= global CPS limit x TTL) plus the dials in flight together
  (<= global concurrency limit), because the check and the attempt claim are not atomic.
  Documented in docs/CHECKPOINT-14-NOTES.md; do not read the cap as a guarantee.

When the cap is reached nothing is acked, discarded or counted against a retry budget: jobs
stay queued and the worker simply stops reading until the budget day rolls over.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.call_attempt import CallAttempt
from app.services.audit_service import record_audit_event
from app.services.calling_window import load_timezone

logger = logging.getLogger("spend_cap")

DAILY_DIAL_CAP_REACHED = "budget_daily_dial_cap_reached"
DAILY_SPEND_CAP_REACHED = "budget_daily_spend_cap_reached"
BUDGET_UNAVAILABLE = "budget_unavailable"

_LOG_INTERVAL_SECONDS = 60.0
_AUDIT_KEY_TTL_SECONDS = 2 * 86_400


@dataclass(frozen=True)
class BudgetStatus:
    day: date
    dials: int
    dial_cap: int
    estimated_minutes: float
    estimated_spend: float | None
    spend_cap: float | None

    @property
    def reached(self) -> str | None:
        if self.dials >= self.dial_cap:
            return DAILY_DIAL_CAP_REACHED
        if (
            self.spend_cap is not None
            and self.estimated_spend is not None
            and self.estimated_spend >= self.spend_cap
        ):
            return DAILY_SPEND_CAP_REACHED
        return None

    @property
    def percent_used(self) -> float:
        ratios = [1.0 if self.dial_cap == 0 else self.dials / self.dial_cap]
        if self.spend_cap:
            ratios.append((self.estimated_spend or 0.0) / self.spend_cap)
        elif self.spend_cap == 0:
            ratios.append(1.0)
        return round(min(max(ratios), 1.0) * 100, 1)


def budget_window(now: datetime, tz_name: str) -> tuple[datetime, datetime, date]:
    """[start, end) of the budget day containing `now`, as UTC instants, plus its local date."""
    tz = load_timezone(tz_name)
    local_date = now.astimezone(tz).date()
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
    end_date = local_date + timedelta(days=1)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC), local_date


def _utcnow() -> datetime:
    return datetime.now(UTC)


def compute_status(db: Session, now: datetime | None = None) -> BudgetStatus:
    """Read today's numbers from PostgreSQL. Raises SQLAlchemyError on a database problem."""
    s = get_settings()
    start, end, day = budget_window(now or _utcnow(), s.budget_timezone)
    unknown_seconds = s.estimated_minutes_per_unknown_attempt * 60
    seconds_each = func.coalesce(CallAttempt.duration_seconds, unknown_seconds)
    dials, seconds = db.execute(
        select(func.count(), func.coalesce(func.sum(seconds_each), 0)).where(
            CallAttempt.started_at >= start, CallAttempt.started_at < end
        )
    ).one()
    minutes = float(seconds) / 60
    spend = (
        round(minutes * s.estimated_cost_per_minute, 4)
        if s.estimated_cost_per_minute is not None
        else None
    )
    return BudgetStatus(
        day=day,
        dials=int(dials),
        dial_cap=s.daily_dial_cap,
        estimated_minutes=round(minutes, 2),
        estimated_spend=spend,
        spend_cap=s.daily_estimated_spend_cap,
    )


# -- cache ----------------------------------------------------------------------------------------

_lock = threading.Lock()
_cache: tuple[float, tuple, BudgetStatus] | None = None  # (monotonic, config key, status)
_last_log = 0.0
_audited: set[tuple[date, str]] = set()


def reset_state() -> None:
    """Forget the cache and the log/audit throttles (tests, and a config reload)."""
    global _cache, _last_log
    with _lock:
        _cache = None
        _last_log = 0.0
        _audited.clear()


def _config_key() -> tuple:
    s = get_settings()
    return (
        s.daily_dial_cap,
        s.daily_estimated_spend_cap,
        s.estimated_cost_per_minute,
        s.estimated_minutes_per_unknown_attempt,
        s.budget_timezone,
    )


def current_status(db: Session, now: datetime | None = None) -> BudgetStatus:
    global _cache
    s = get_settings()
    moment = now or _utcnow()
    key = _config_key()
    day = moment.astimezone(load_timezone(s.budget_timezone)).date()
    with _lock:
        cached = _cache
    if (
        cached is not None
        and s.budget_check_cache_ttl_seconds > 0
        and time.monotonic() - cached[0] < s.budget_check_cache_ttl_seconds
        and cached[1] == key
        and cached[2].day == day
    ):
        return cached[2]
    status = compute_status(db, moment)
    with _lock:
        _cache = (time.monotonic(), key, status)
    return status


def block_reason(db: Session, now: datetime | None = None) -> str | None:
    """None -> within budget. Otherwise a reason code. Never raises on a database problem:
    an unreadable budget is reported as BUDGET_UNAVAILABLE (fail closed)."""
    try:
        status = current_status(db, now)
    except SQLAlchemyError:
        logger.error("budget_unreadable_failing_closed")
        return BUDGET_UNAVAILABLE
    reason = status.reached
    if reason is not None:
        _note_reached(reason, status)
    return reason


def _note_reached(reason: str, status: BudgetStatus) -> None:
    """One log line per minute; one audit event per budget day per cap."""
    global _last_log
    now = time.monotonic()
    with _lock:
        if now - _last_log >= _LOG_INTERVAL_SECONDS:
            _last_log = now
            logger.warning(
                "budget_cap_reached_not_dialing",
                extra={
                    "reason": reason,
                    "budget_day": status.day.isoformat(),
                    "dials": status.dials,
                },
            )
        if (status.day, reason) in _audited:
            return
    _audit_once(reason, status)


def _audit_once(reason: str, status: BudgetStatus) -> None:
    """Cross-process dedupe through one Redis SET NX; the row goes through its own short
    session so it commits independently of the job's transaction. Best effort: if Redis is
    down the cap still blocks (logged), only the audit row is skipped."""
    try:
        from app.core.redis_client import get_redis

        first = get_redis().set(
            f"budget:audit:{status.day.isoformat()}:{reason}", "1",
            nx=True, ex=_AUDIT_KEY_TTL_SECONDS,
        )  # fmt: skip
        if first:
            with SessionLocal() as session:
                record_audit_event(
                    session,
                    actor="dialer-worker",
                    action="spend_cap.reached",
                    entity_type="system",
                    entity_id=None,
                    metadata={
                        "cap": reason,
                        "budget_day": status.day.isoformat(),
                        "dials": status.dials,
                        "dial_cap": status.dial_cap,
                        "estimated_spend": status.estimated_spend,
                        "spend_cap": status.spend_cap,
                    },
                )
                session.commit()
        with _lock:
            _audited.add((status.day, reason))
    except Exception:  # noqa: BLE001 -- an audit failure must never stop the gate answering
        logger.exception("spend_cap_audit_failed")
