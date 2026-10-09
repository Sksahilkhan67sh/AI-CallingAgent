"""Calling-window arithmetic -- the ONE place a time of day is compared to a window (CP14, C5a).

Before CP14 the window was compared to the UTC clock in two places, so "10:00-18:00" really
meant 15:30-23:30 IST and an overnight window never matched. Everything here is pure:
`now` is always passed in (aware, any zone), nothing reads the system clock.

Semantics
* A window is half-open: `start <= local_time < end`, in the CAMPAIGN's timezone.
* `start > end` is an overnight window (22:00-06:00). `start == end` is invalid.
* The global HARD bound is a compliance backstop. A campaign window can never extend past
  it: it is rejected on save (`window_within_bound`) AND clamped at evaluation time
  (`is_dialable_now` requires BOTH windows to be open), so a legacy or hand-edited row
  cannot dial outside it either.
* Timezones are IANA names resolved with `zoneinfo`. There is no fixed-offset fallback: a
  zone that cannot load is an error, never a silent guess.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAY = 86_400.0
_Interval = tuple[float, float]


class InvalidTimezoneError(ValueError):
    pass


class NoDialableWindowError(ValueError):
    """The campaign window and the hard bound never overlap: there is no time to dial."""


def load_timezone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise InvalidTimezoneError("timezone must be a non-empty IANA name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise InvalidTimezoneError("unknown or unloadable IANA timezone") from exc


def _seconds(t: time) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def _intervals(start: time, end: time) -> list[_Interval]:
    s, e = _seconds(start), _seconds(end)
    if s == e:
        raise ValueError("window start and end must differ")
    if s < e:
        return [(s, e)]
    return [(s, _DAY)] + ([(0.0, e)] if e > 0 else [])  # overnight


def _intersect(a: list[_Interval], b: list[_Interval]) -> list[_Interval]:
    out = [
        (max(s1, s2), min(e1, e2)) for s1, e1 in a for s2, e2 in b if max(s1, s2) < min(e1, e2)
    ]
    return sorted(out)


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def is_within_calling_window(now_utc: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    """Is `now` inside [start, end) as read on the wall clock of `tz`?"""
    local = _aware(now_utc).astimezone(tz)
    t = _seconds(local.time())
    return any(s <= t < e for s, e in _intervals(start, end))


def is_dialable_now(
    now_utc: datetime,
    tz: ZoneInfo,
    start: time,
    end: time,
    hard_start: time,
    hard_end: time,
) -> bool:
    """The campaign window AND the global hard bound must both be open (the clamp)."""
    return is_within_calling_window(now_utc, tz, start, end) and is_within_calling_window(
        now_utc, tz, hard_start, hard_end
    )


def window_within_bound(start: time, end: time, hard_start: time, hard_end: time) -> bool:
    """Is every instant of [start, end) also inside the hard bound? (Save-time validation.)"""
    bound = _intervals(hard_start, hard_end)
    return all(
        any(bs <= s and e <= be for bs, be in bound) for s, e in _intervals(start, end)
    )


def next_window_open(
    now_utc: datetime,
    tz: ZoneInfo,
    start: time,
    end: time,
    hard_start: time,
    hard_end: time,
) -> datetime:
    """The earliest instant >= now at which dialing is allowed (UTC). Returns `now` itself
    when the window is already open. Raises NoDialableWindowError if it can never open."""
    now_utc = _aware(now_utc).astimezone(UTC)
    if is_dialable_now(now_utc, tz, start, end, hard_start, hard_end):
        return now_utc

    open_spans = _intersect(_intervals(start, end), _intervals(hard_start, hard_end))
    if not open_spans:
        raise NoDialableWindowError("campaign window does not overlap the hard calling window")

    local_today = now_utc.astimezone(tz).date()
    best: datetime | None = None
    for day in range(0, 3):  # today, tomorrow, and one spare for a DST shift
        date = local_today + timedelta(days=day)
        for span_start, _ in open_spans:
            whole = int(span_start)
            local_open = datetime(
                date.year, date.month, date.day,
                whole // 3600, whole % 3600 // 60, whole % 60,
                int(round((span_start - whole) * 1e6)),
                tzinfo=tz,
            )  # fmt: skip
            candidate = local_open.astimezone(UTC)
            # A spring-forward gap can land the opening just before the real window start.
            for _ in range(180):
                if is_dialable_now(candidate, tz, start, end, hard_start, hard_end):
                    break
                candidate += timedelta(minutes=1)
            if candidate > now_utc and (best is None or candidate < best):
                best = candidate
    if best is None:  # pragma: no cover -- 3 days always contain an opening
        raise NoDialableWindowError("no opening found within three days")
    return best
