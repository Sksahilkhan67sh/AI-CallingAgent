# ruff: noqa: F811
"""CP14 (C5a) -- calling windows are read in the campaign's timezone, clamped by a hard bound,
and a job outside its window is deferred, never lost."""

import ast
import json
import pathlib
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.suppression_repository import SuppressionRepository
from app.services.calling_window import (
    InvalidTimezoneError,
    NoDialableWindowError,
    is_dialable_now,
    is_within_calling_window,
    load_timezone,
    next_window_open,
    window_within_bound,
)
from app.services.eligibility_service import DialEligibilityService
from app.services.queue.dialer_worker import JobOutcome
from app.services.queue.enqueue_service import enqueue_guard_key
from app.services.queue.job import DialJob
from app.services.recovery.dispatch import dispatch_due_recovery_jobs
from app.services.recovery.job import RecoveryJob
from tests.cp14_helpers import (
    IST,
    Session,
    commit_world,
    ist,
    production_window,  # noqa: F401  (fixture)
    rig,  # noqa: F401  (fixture)
    truncate_after_module,  # noqa: F401  (fixture)
)
from tests.cp14_helpers import attempts_of as _attempts
from tests.cp14_helpers import contact_state as _contact
from tests.cp14_helpers import enqueue_first as _enqueue_first

NY = ZoneInfo("America/New_York")
H9, H21 = time(9), time(21)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# -- the pure function --------------------------------------------------------------------------


def test_window_is_read_on_the_campaign_clock_not_utc():
    # 04:30Z is 10:00 IST: a 10:00-18:00 window is OPEN (UTC says it is closed).
    assert is_within_calling_window(utc(2026, 1, 5, 4, 30), IST, time(10), time(18))
    # 13:00Z is 18:30 IST: CLOSED (UTC says it is open).
    assert not is_within_calling_window(utc(2026, 1, 5, 13, 0), IST, time(10), time(18))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 1, 5, 4, 29, 59), False),  # 09:59:59 IST
        (utc(2026, 1, 5, 4, 30, 0), True),  # 10:00:00 IST  (start inclusive)
        (utc(2026, 1, 5, 12, 29, 59), True),  # 17:59:59 IST
        (utc(2026, 1, 5, 12, 30, 0), False),  # 18:00:00 IST  (end exclusive)
    ],
)
def test_window_boundaries_are_half_open(now, expected):
    assert is_within_calling_window(now, IST, time(10), time(18)) is expected


def test_overnight_window_matches_across_midnight():
    start, end = time(22), time(6)
    inside = [ist(22, 0), ist(23, 59), ist(0, 0), ist(5, 59)]
    outside = [ist(6, 0), ist(12, 0), ist(21, 59)]
    assert all(is_within_calling_window(t, IST, start, end) for t in inside)
    assert not any(is_within_calling_window(t, IST, start, end) for t in outside)


def test_overnight_window_ending_at_midnight():
    assert is_within_calling_window(ist(23, 30), IST, time(22), time(0))
    assert not is_within_calling_window(ist(0, 0, day=7), IST, time(22), time(0))


def test_equal_start_and_end_is_invalid():
    with pytest.raises(ValueError):
        is_within_calling_window(ist(12), IST, time(9), time(9))


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        is_within_calling_window(datetime(2026, 1, 5, 12), IST, H9, H21)


def test_dst_zone_is_read_correctly_either_side_of_the_change():
    # New York springs forward 2026-03-08 02:00 -> 03:00. 13:30Z is 08:30 EST the day before
    # but 09:30 EDT after: the same UTC wall time falls on different sides of a 09:00 start.
    assert not is_within_calling_window(utc(2026, 3, 7, 13, 30), NY, H9, H21)  # 08:30 EST
    assert is_within_calling_window(utc(2026, 3, 8, 13, 30), NY, H9, H21)  # 09:30 EDT
    # And the fall change (2026-11-01): 13:30Z is 09:30 EDT before, 08:30 EST after.
    assert is_within_calling_window(utc(2026, 10, 31, 13, 30), NY, H9, H21)
    assert not is_within_calling_window(utc(2026, 11, 1, 13, 30), NY, H9, H21)


def test_next_open_across_a_dst_change():
    # 23:00 EST on 7 Mar -> 09:00 EDT on 8 Mar = 13:00Z (not 14:00Z).
    assert next_window_open(utc(2026, 3, 8, 4, 0), NY, H9, H21, H9, H21) == utc(2026, 3, 8, 13, 0)


def test_next_open_when_already_open_is_now_and_otherwise_the_next_start():
    now = ist(12)
    assert next_window_open(now, IST, H9, H21, H9, H21) == now
    assert next_window_open(ist(21, 30), IST, H9, H21, H9, H21) == ist(9, 0, day=7)
    assert next_window_open(ist(3, 0), IST, H9, H21, H9, H21) == ist(9, 0)


def test_hard_bound_clamps_a_campaign_window_at_evaluation_time():
    # A (hand-edited) 08:00-22:00 window can never dial before 09:00 or after 21:00.
    args = (IST, time(8), time(22), H9, H21)
    assert not is_dialable_now(ist(8, 30), *args)
    assert is_dialable_now(ist(9, 0), *args)
    assert is_dialable_now(ist(20, 59), *args)
    assert not is_dialable_now(ist(21, 30), *args)
    assert next_window_open(ist(8, 30), *args) == ist(9, 0)


def test_window_within_bound_validation():
    assert window_within_bound(time(10), time(18), H9, H21)
    assert window_within_bound(H9, H21, H9, H21)
    assert not window_within_bound(time(8), time(18), H9, H21)
    assert not window_within_bound(time(10), time(22), H9, H21)
    assert not window_within_bound(time(22), time(6), H9, H21)  # overnight can't fit 09-21


def test_a_window_that_never_overlaps_the_bound_cannot_open():
    with pytest.raises(NoDialableWindowError):
        next_window_open(ist(12), IST, time(22), time(23), H9, H21)


def test_timezone_loading_never_falls_back_to_an_offset():
    assert load_timezone("Asia/Kolkata").key == "Asia/Kolkata"
    for bad in ["", "   ", "IST", "Mars/Base", "../etc/passwd", "/etc/passwd", "x" * 80, None]:
        with pytest.raises(InvalidTimezoneError):
            load_timezone(bad)  # type: ignore[arg-type]


# -- no call path may compare against UTC -----------------------------------------------------------

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def test_no_module_reads_a_time_of_day_except_the_shared_window_module():
    """Static proof: `.time()` (the old `now.time()` UTC comparison) appears only in
    calling_window.py, and the policy's window columns are read only by the policy service."""
    time_calls, window_reads = [], []
    for path in _APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(_APP))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and not node.args
                # time.time() is the stdlib epoch clock, not a time-of-day read
                and not (isinstance(node.func.value, ast.Name) and node.func.value.id == "time")
            ):
                time_calls.append(rel)
            if isinstance(node, ast.Attribute) and node.attr in {"window_start", "window_end"}:
                window_reads.append(rel)
    assert set(time_calls) <= {"services/calling_window.py"}, time_calls
    assert set(window_reads) <= {
        "services/retry_policy_service.py",  # the one adapter to the shared functions
        "models/retry_policy.py",
    }, window_reads


def _eligibility(campaign, contact, policy, now):
    with Session() as s:
        c = s.get(Campaign, campaign)
        k = s.get(Contact, contact)
        return DialEligibilityService(SuppressionRepository(s)).check(k, c, policy, now=now)


def test_eligibility_reads_the_campaign_timezone(production_window):
    sydney_campaign, (sydney_contact,) = commit_world(timezone="Australia/Sydney")
    india_campaign, (india_contact,) = commit_world(timezone="Asia/Kolkata")
    # 04:30Z: 10:00 IST (open) but 15:30 AEDT (open) -- pick an instant where they differ:
    now = utc(2026, 1, 5, 2, 0)  # 07:30 IST (closed), 13:00 AEDT (open)
    assert _eligibility(india_campaign, india_contact, None, now).eligible is False
    assert _eligibility(sydney_campaign, sydney_contact, None, now).eligible is True


def test_a_legacy_campaign_without_a_policy_row_is_blocked_outside_the_window(production_window):
    campaign, (contact,) = commit_world(policy=None)
    closed = _eligibility(campaign, contact, None, ist(3, 0))
    assert closed.eligible is False and closed.code == "outside_calling_window"
    assert closed.reason == "Outside calling window" and closed.transient
    assert _eligibility(campaign, contact, None, ist(12, 0)).eligible is True


def test_region_is_rechecked_at_dial_time_for_legacy_rows(production_window):
    campaign, (contact,) = commit_world()
    with Session() as s:
        s.get(Contact, contact).normalized_phone_number = "+19876543210"  # the C4 legacy value
        s.commit()
    result = _eligibility(campaign, contact, None, ist(12, 0))
    assert result.eligible is False and result.code == "region_not_allowed"
    assert not result.transient  # permanent: this is not a "try again later"


# -- the dialer: deferred, never lost -------------------------------------------------------------------


def test_first_attempt_outside_the_window_is_deferred_not_lost(rig):
    campaign, (contact,) = commit_world(policy=None)  # legacy campaign: default 09-21 IST
    job = _enqueue_first(rig, campaign, contact)

    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED

    # Nothing dialed, nothing claimed, nothing consumed:
    assert _attempts(contact) == []
    assert _contact(contact) == (ContactStatus.PENDING, 0)
    # The stream message is released ONLY because the job now lives in the scheduler...
    assert rig.pending() == 0
    ((member, score),) = rig.scheduled()
    assert score == ist(9, 0).timestamp()  # ...due at the next opening, 09:00 IST
    assert json.loads(member)["kind"] == "dial_job"
    assert json.loads(member)["job_id"] == job.job_id
    # ...and PostgreSQL can rebuild it: the enqueue guard is gone (CP12-B pattern).
    assert rig.redis.get(enqueue_guard_key(job.idempotency_key)) is None


def test_deferred_job_dials_exactly_once_after_the_window_opens(rig):
    campaign, (contact,) = commit_world(policy=None)
    _enqueue_first(rig, campaign, contact)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED

    # Before the opening: nothing is due, nothing dials.
    rig.clock.now = ist(8, 59)
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now) == 0
    assert rig.run_one() == JobOutcome.NO_JOB
    assert _attempts(contact) == []

    # The window opens: the scheduler hands it back, the dialer dials it once.
    rig.clock.now = ist(9, 1)
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now) == 1
    assert rig.run_one() == JobOutcome.ADMITTED_AND_DIALED
    assert len(_attempts(contact)) == 1
    assert _contact(contact)[1] == 1

    # Nothing is left anywhere to dial it a second time.
    assert rig.scheduled() == []
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now) == 0
    assert rig.run_one() == JobOutcome.NO_JOB
    assert len(_attempts(contact)) == 1


def test_a_backlog_outside_the_window_is_all_deferred_and_all_dials_once_later(rig):
    campaign, ids = commit_world(contacts=12, policy=None)
    for c in ids:
        _enqueue_first(rig, campaign, c)
    assert [rig.run_one() for _ in ids] == [JobOutcome.WINDOW_DEFERRED] * 12
    assert len(rig.scheduled()) == 12 and rig.pending() == 0
    assert all(_attempts(c) == [] for c in ids)

    rig.clock.now = ist(9, 30)
    dispatched = 0
    while True:
        n = dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now)
        dispatched += n
        if not n:
            break
    assert dispatched == 12
    outcomes = [rig.run_one() for _ in range(12)]
    assert outcomes == [JobOutcome.ADMITTED_AND_DIALED] * 12
    assert all(len(_attempts(c)) == 1 for c in ids)


def test_deferring_the_same_job_twice_is_one_scheduler_entry(rig):
    campaign, (contact,) = commit_world(policy=None)
    job = _enqueue_first(rig, campaign, contact)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED
    rig.queue.enqueue(job)  # the very same job delivered again (duplicate delivery)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED
    assert len(rig.scheduled()) == 1


def test_nothing_is_acked_if_the_scheduler_write_fails(rig, monkeypatch):
    campaign, (contact,) = commit_world(policy=None)
    job = _enqueue_first(rig, campaign, contact)

    def boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(type(rig.scheduler), "schedule_dial", boom)
    with pytest.raises(RuntimeError):
        rig.run_one()
    assert rig.pending() == 1  # still in the pending list, will be re-driven
    assert rig.redis.get(enqueue_guard_key(job.idempotency_key)) == "1"  # guard untouched
    assert _contact(contact) == (ContactStatus.PENDING, 0)


def test_a_retry_that_arrives_after_the_window_closed_goes_back_to_the_scheduler(rig):
    campaign, (contact,) = commit_world(policy={"window_start": H9, "window_end": H21})
    previous = "11111111-1111-1111-1111-111111111111"
    retry = DialJob.new(
        campaign_id=campaign,
        contact_id=contact,
        attempt_number=2,
        recovery_type="RECONNECT",
        previous_attempt_id=previous,
    )
    rig.queue.enqueue(retry)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED
    assert rig.pending() == 0
    ((member, score),) = rig.scheduled()
    restored = RecoveryJob.from_json(member)  # a normal RecoveryJob: the retry's usual home
    assert (restored.attempt_id, restored.attempt_number) == (previous, 2)
    assert score == ist(9, 0).timestamp()


def test_a_job_that_cannot_be_moved_safely_is_held_unacked(rig):
    campaign, (contact,) = commit_world(policy=None)
    # attempt 2 with no recovery context: nothing to rebuild it from -> hold, never drop.
    rig.queue.enqueue(DialJob.new(campaign_id=campaign, contact_id=contact, attempt_number=2))
    assert rig.run_one() == JobOutcome.WINDOW_HELD
    assert rig.pending() == 1 and rig.scheduled() == []
    assert _attempts(contact) == []


def test_a_corrupt_timezone_holds_the_job_and_never_dials(rig):
    campaign, (contact,) = commit_world(policy=None)
    with Session() as s:
        s.get(Campaign, campaign).timezone = "Nowhere/Land"  # bypassing API validation
        s.commit()
    _enqueue_first(rig, campaign, contact)
    rig.clock.now = ist(12, 0)  # would be open in IST: a guess must not be made
    assert rig.run_one() == JobOutcome.WINDOW_HELD
    assert rig.pending() == 1 and _attempts(contact) == []


def test_hard_bound_stops_a_hand_edited_window_from_dialing_early(rig):
    campaign, (contact,) = commit_world(policy={"window_start": time(8), "window_end": time(22)})
    _enqueue_first(rig, campaign, contact)
    rig.clock.now = ist(8, 30)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED  # open per the row, closed per the bound
    ((_, score),) = rig.scheduled()
    assert score == ist(9, 0).timestamp()


def test_a_number_suppressed_while_deferred_is_never_dialed(rig):
    from app.models.enums import SuppressionSource
    from app.models.suppression import Suppression

    campaign, (contact,) = commit_world(policy=None)
    _enqueue_first(rig, campaign, contact)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED
    with Session() as s:
        number = s.get(Contact, contact).normalized_phone_number
        s.add(Suppression(phone_number=number, source=SuppressionSource.MANUAL_API))
        s.commit()
    rig.clock.now = ist(9, 5)
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now) == 0
    assert rig.run_one() == JobOutcome.NO_JOB
    assert _attempts(contact) == []


def test_a_paused_campaign_keeps_its_deferred_job_until_resumed(rig):
    campaign, (contact,) = commit_world(policy=None)
    _enqueue_first(rig, campaign, contact)
    assert rig.run_one() == JobOutcome.WINDOW_DEFERRED
    with Session() as s:
        s.get(Campaign, campaign).status = CampaignStatus.PAUSED
        s.commit()
    rig.clock.now = ist(9, 5)
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=rig.clock.now) == 0
    ((_, score),) = rig.scheduled()  # still scheduled (re-checked in a minute), not dropped
    assert score == pytest.approx((rig.clock.now + timedelta(seconds=60)).timestamp())


def test_recovery_dispatch_reschedules_retries_to_the_next_opening_in_local_time(rig):
    campaign, (contact,) = commit_world(policy={"window_start": time(10), "window_end": time(18)})
    job = RecoveryJob.new(
        attempt_id=__import__("uuid").UUID("22222222-2222-2222-2222-222222222222"),
        contact_id=contact,
        campaign_id=campaign,
        attempt_number=2,
    )
    rig.scheduler.schedule(job, utc(2026, 1, 5, 0, 0))
    # 13:00Z = 18:30 IST: closed by IST, "open" by the old UTC comparison.
    assert dispatch_due_recovery_jobs(rig.scheduler, rig.queue, now=utc(2026, 1, 5, 13, 0)) == 0
    ((_, score),) = rig.scheduled()
    assert score == utc(2026, 1, 6, 4, 30).timestamp()  # 10:00 IST the next day
