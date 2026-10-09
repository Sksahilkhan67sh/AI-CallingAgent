"""CP12-C -- complete, keyset-paginated, restart-safe enqueue (H2), on REAL PostgreSQL + Redis.

The H2 reproduction is `test_workers_consuming_during_the_scan_cannot_make_it_skip_contacts`:
contacts leaving PENDING while the scan runs (what dialing workers do) used to shift the
OFFSET window and skip rows. Failure/restart tests inject faults at exact points and then
re-run for real. What is NOT proven here: throughput at 100K (see
scripts/bench_enqueue_pagination.py and docs/CHECKPOINT-12C-NOTES.md for measured numbers).
"""

import itertools
import threading
import tracemalloc
import uuid

import pytest
import redis as redis_lib
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.repositories.contact_repository import ContactRepository
from app.services import kill_switch
from app.services.queue import enqueue_service
from app.services.queue.enqueue_service import QueueEnqueueService, enqueue_guard_key
from app.services.queue.factory import get_queue
from app.services.queue.job import DialJob
from app.services.queue.redis_queue import RedisStreamQueue
from tests.test_cp11_concurrency import (
    N_WORKERS,
    _attempts_by_contact,
    _drain,
    _engine,
    _run_threads,
    _Session,
    _SpyProvider,
)
from tests.test_cp12b_paused_jobs import (
    _api,
    _contacts,
    _enqueue,
    _set_status,
    _status,
    _stop_after,
)

LEASE_GLOBAL = "concurrency:lease:global"


@pytest.fixture(scope="module", autouse=True)
def _truncate_after_module():
    yield
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def _fresh_state(redis_client, monkeypatch):
    redis_client.flushdb()
    settings = get_settings()
    monkeypatch.setattr(settings, "enqueue_rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "mutation_rate_limit_per_minute", 10_000)
    yield
    redis_client.flushdb()
    with _engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM suppression")


# -- helpers --------------------------------------------------------------------------------------


def _page_size(monkeypatch, size: int) -> None:
    monkeypatch.setattr(get_settings(), "enqueue_page_size", size)


# Suppression is global by phone number (correct product behaviour), so every contact this
# module creates gets a number no other test can ever suppress.
_PHONE_BLOCKS = itertools.count(1)


def _bulk(campaign_id, count, *, start=0, status=ContactStatus.PENDING, attempt_count=0, ids=None):
    block = next(_PHONE_BLOCKS) * 1_000_000
    rows = [
        {
            "id": ids[i] if ids else uuid.uuid4(),
            "campaign_id": campaign_id,
            # A valid Indian mobile (98 + 8 digits), unique per (block, start, i): the dial-time
            # region re-check (CP14) refuses numbers that are not real E.164.
            "phone_number": f"98{block + start + i:08d}",
            "normalized_phone_number": f"+9198{block + start + i:08d}",
            "status": status,
            "attempt_count": attempt_count,
        }
        for i in range(count)
    ]
    if rows:  # an empty executemany would insert one all-defaults row
        with _engine.begin() as conn:
            conn.execute(Contact.__table__.insert(), rows)
    return [r["id"] for r in rows]


def _campaign(count=0, **kw):
    with _Session() as s:
        from app.models.campaign import Campaign

        campaign = Campaign(name="cp12c", status=kw.pop("status", CampaignStatus.ACTIVE))
        s.add(campaign)
        s.commit()
        campaign_id = campaign.id
    return campaign_id, _bulk(campaign_id, count, **kw)


def _run(campaign_id):
    """The service exactly as the route builds it, on a fresh session (a 'fresh process')."""
    with _Session() as s:
        result = QueueEnqueueService(s, get_queue(), actor="test").enqueue_campaign(campaign_id)
        s.commit()
    return result


def _queued_keys(redis_client) -> list[str]:
    entries = redis_client.xrange(get_queue().stream_key)
    return [DialJob.from_json(fields["job"]).idempotency_key for _id, fields in entries]


def _expected_keys(campaign_id, contact_ids) -> set[str]:
    return {f"{campaign_id}:{c}:1" for c in contact_ids}


def _assert_queue_is_exactly(redis_client, campaign_id, contact_ids):
    keys = _queued_keys(redis_client)
    assert len(keys) == len(set(keys)), "duplicate logical jobs in the stream"
    assert set(keys) == _expected_keys(campaign_id, contact_ids)


class _PageSpy:
    """Records every keyset page; optional hook runs BEFORE page number n (1-based)."""

    def __init__(self, monkeypatch, hook=None, transform=None):
        self.calls: list[tuple[uuid.UUID | None, list[uuid.UUID]]] = []
        real = ContactRepository.eligible_enqueue_page
        spy = self

        def wrapper(repo, campaign_id, *, after_id, limit):
            if hook:
                hook(len(spy.calls) + 1, after_id)
            rows = list(real(repo, campaign_id, after_id=after_id, limit=limit))
            if transform:
                rows = transform(len(spy.calls) + 1, rows)
            spy.calls.append((after_id, [r[0] for r in rows]))
            return rows

        monkeypatch.setattr(ContactRepository, "eligible_enqueue_page", wrapper)

    @property
    def visited(self) -> list[uuid.UUID]:
        return [i for _after, ids in self.calls for i in ids]


# -- 1-5, 7, 22, 23: sizes and boundaries ---------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "page", "pages_with_rows"),
    [
        (7, 50, 1),  # small: fewer than one page
        (50, 50, 1),  # exact page boundary
        (51, 50, 2),  # PAGE_SIZE + 1: the second page must be processed
        (1200, 500, 3),  # the H2 report: 1200 contacts, page 500
        (100, 50, 2),  # a multiple of the page size: the extra EMPTY page ends the scan
    ],
)
def test_every_eligible_contact_is_queued_across_page_boundaries(
    redis_client, monkeypatch, count, page, pages_with_rows
):
    _page_size(monkeypatch, page)
    campaign_id, contact_ids = _campaign(count)
    spy = _PageSpy(monkeypatch)

    result = _run(campaign_id)

    assert (result.discovered, result.enqueued, result.complete) == (count, count, True)
    assert result.skipped_suppressed == result.skipped_duplicate == 0
    assert result.pages_processed == pages_with_rows
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)
    # the scan ended on an EMPTY page, never on a short one (even an exact multiple needs it)
    assert [len(ids) for _a, ids in spy.calls][-1] == 0
    assert len(spy.calls) == pages_with_rows + 1


def test_a_campaign_with_no_eligible_contacts_returns_zeroes(redis_client):
    campaign_id, _ = _campaign(0)
    result = _run(campaign_id)
    assert (result.discovered, result.enqueued, result.pages_processed, result.complete) == (
        0,
        0,
        0,
        True,
    )
    assert _queued_keys(redis_client) == []


def test_ten_thousand_contacts_all_queued_with_no_truncation(redis_client):
    campaign_id, contact_ids = _campaign(10_000)
    result = _run(campaign_id)
    assert (result.discovered, result.enqueued, result.complete) == (10_000, 10_000, True)
    assert result.pages_processed == 20  # default page size 500
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


def test_paging_is_bounded_and_visits_each_contact_once_in_key_order(redis_client, monkeypatch):
    """The bounded-paging proof used in place of a 100K unit test: 5,000 contacts through a
    page of 64 means 79 queries, none returning more than 64 rows, ids strictly increasing."""
    _page_size(monkeypatch, 64)
    campaign_id, contact_ids = _campaign(5_000)
    spy = _PageSpy(monkeypatch)

    _run(campaign_id)

    sizes = [len(ids) for _a, ids in spy.calls]
    assert max(sizes) <= 64 and len(spy.calls) == 5_000 // 64 + 1 + 1  # 78 full+1 short, +empty
    visited = spy.visited
    assert visited == sorted(visited) and len(visited) == len(set(visited)) == 5_000
    assert set(visited) == set(contact_ids)
    # each page resumes exactly after the last row of the page before it
    assert [a for a, _ in spy.calls[1:]] == [ids[-1] for _a, ids in spy.calls[:-1] if ids]


# -- H2 root cause --------------------------------------------------------------------------------


def test_workers_consuming_during_the_scan_cannot_make_it_skip_contacts(redis_client, monkeypatch):
    """H2: while the scan runs, dialing workers move already-queued contacts out of PENDING.
    With OFFSET paging that shrank the filtered set under the offset and rows were skipped
    (~700 of 1200 queued). The keyset cursor does not care."""
    _page_size(monkeypatch, 100)
    campaign_id, contact_ids = _campaign(1_200)
    seen: list[uuid.UUID] = []

    def workers_dial_what_was_queued(page_no, _after):
        if page_no > 1:  # everything queued so far has been picked up and is now being dialed
            queued = [uuid.UUID(k.split(":")[1]) for k in _queued_keys(redis_client)]
            fresh = [c for c in queued if c not in seen]
            seen.extend(fresh)
            with _Session() as s:
                s.execute(
                    update(Contact)
                    .where(Contact.id.in_(fresh))
                    .values(status=ContactStatus.DIALING)
                )
                s.commit()

    _PageSpy(monkeypatch, hook=workers_dial_what_was_queued)

    result = _run(campaign_id)

    assert result.enqueued == 1_200 and result.discovered == 1_200 and result.complete
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


def test_contacts_inserted_during_the_scan_are_never_duplicated_and_the_next_run_gets_the_rest(
    redis_client, monkeypatch
):
    _page_size(monkeypatch, 50)
    campaign_id, original = _campaign(300)
    late: list[uuid.UUID] = []

    def insert_once(page_no, _after):
        if page_no == 3:
            late.extend(_bulk(campaign_id, 80, start=900_000))

    _PageSpy(monkeypatch, hook=insert_once)
    first = _run(campaign_id)

    assert first.complete and first.enqueued >= 300  # every original; some late rows may be seen
    keys = _queued_keys(redis_client)
    assert len(keys) == len(set(keys))  # never twice
    assert _expected_keys(campaign_id, original) <= set(keys)

    second = _run(campaign_id)  # a later run completes the picture, idempotently
    assert second.enqueued + first.enqueued == 300 + 80
    _assert_queue_is_exactly(redis_client, campaign_id, [*original, *late])


# -- 11, 12, 22-24: cursor, order, short pages, id range ------------------------------------------


def test_a_short_page_in_the_middle_does_not_end_the_scan(redis_client, monkeypatch):
    """A page smaller than the page size is not the end. Page 2 is cut to 3 rows; the scan
    must keep going from the last row it actually handled."""
    _page_size(monkeypatch, 50)
    campaign_id, contact_ids = _campaign(120)

    def shorten_second(page_no, rows):
        return rows[:3] if page_no == 2 else rows

    spy = _PageSpy(monkeypatch, transform=shorten_second)
    result = _run(campaign_id)

    assert result.discovered == result.enqueued == 120 and result.complete
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)
    assert len(spy.visited) == len(set(spy.visited)) == 120  # nothing repeated, nothing skipped


def test_uuid_extremes_are_paged_in_database_order(redis_client, monkeypatch):
    _page_size(monkeypatch, 3)
    ids = [
        uuid.UUID("00000000-0000-0000-0000-000000000001"),
        uuid.UUID("7fffffff-ffff-ffff-ffff-ffffffffffff"),
        uuid.UUID("80000000-0000-0000-0000-000000000000"),
        uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffffe"),
        uuid.UUID("0000000f-0000-0000-0000-000000000000"),
        uuid.UUID("f0000000-0000-0000-0000-000000000000"),
        uuid.UUID("00000000-0000-0000-0000-00000000000f"),
        *[uuid.uuid4() for _ in range(10)],
    ]
    campaign_id, _ = _campaign(0)
    _bulk(campaign_id, len(ids), ids=ids)
    spy = _PageSpy(monkeypatch)

    result = _run(campaign_id)

    assert result.enqueued == len(ids)
    assert spy.visited == sorted(ids)  # python and PostgreSQL agree on the order
    _assert_queue_is_exactly(redis_client, campaign_id, ids)


# -- 8: mixed eligibility -------------------------------------------------------------------------


def test_mixed_eligibility_counts_are_truthful(redis_client, monkeypatch):
    _page_size(monkeypatch, 4)
    campaign_id, _ = _campaign(0)
    eligible = _bulk(campaign_id, 10, start=0)
    guarded = _bulk(campaign_id, 2, start=100)  # eligible but already queued
    for c in guarded:
        redis_client.set(enqueue_guard_key(f"{campaign_id}:{c}:1"), "1", ex=3600)
    suppressed = _bulk(campaign_id, 3, start=200)
    _bulk(campaign_id, 4, start=300, attempt_count=1)  # already attempted
    _bulk(campaign_id, 5, start=400, status=ContactStatus.CLOSED)  # invalid/closed
    _bulk(campaign_id, 6, start=500, status=ContactStatus.COMPLETED)
    with _Session() as s:
        numbers = dict(
            s.execute(
                select(Contact.id, Contact.normalized_phone_number).where(
                    Contact.id.in_(suppressed)
                )
            ).all()
        )
        for c in suppressed:
            s.add(
                Suppression(
                    contact_id=c, phone_number=numbers[c], source=next(iter(SuppressionSource))
                )
            )
        s.commit()

    result = _run(campaign_id)

    assert result.enqueued == 10
    assert result.skipped_suppressed == 3
    assert result.skipped_duplicate == 2
    assert (
        result.discovered
        == 15
        == result.enqueued + result.skipped_suppressed + result.skipped_duplicate
    )
    assert set(_queued_keys(redis_client)) == _expected_keys(campaign_id, eligible)


def test_a_paused_campaign_cannot_be_enqueued(redis_client):
    campaign_id, _ = _campaign(5, status=CampaignStatus.PAUSED)
    assert _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue").status_code == 422
    assert _queued_keys(redis_client) == []


# -- 9, 10, 19: idempotency and concurrency -------------------------------------------------------


def test_repeated_enqueue_creates_no_duplicate_jobs(redis_client, monkeypatch):
    _page_size(monkeypatch, 40)
    campaign_id, contact_ids = _campaign(130)
    first, second, third = _run(campaign_id), _run(campaign_id), _run(campaign_id)
    assert first.enqueued == 130
    assert (second.enqueued, second.skipped_duplicate, second.discovered) == (0, 130, 130)
    assert (third.enqueued, third.skipped_duplicate) == (0, 130)
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


def test_concurrent_enqueues_of_one_campaign_queue_every_contact_exactly_once(
    redis_client, monkeypatch
):
    _page_size(monkeypatch, 60)
    campaign_id, contact_ids = _campaign(600)
    bodies: list[dict] = [{}] * N_WORKERS

    def go(i):
        response = _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue")
        assert response.status_code == 200, response.text
        bodies[i] = response.json()

    _run_threads(go)

    assert sum(b["enqueued"] for b in bodies) == 600  # each contact won exactly one race
    assert all(b["discovered"] == 600 and b["complete"] for b in bodies)
    assert all(b["enqueued"] + b["skipped_duplicate"] == 600 for b in bodies)
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


# -- 13-16: failures stay truthful and restart-safe -----------------------------------------------


def _fail_enqueue_after(monkeypatch, succeed: int, exc=None):
    real = RedisStreamQueue.enqueue_once
    count = {"n": 0}

    def flaky(queue, job, guard_key, ttl):
        count["n"] += 1
        if count["n"] > succeed:
            raise exc or redis_lib.ConnectionError("redis went away mid-page")
        return real(queue, job, guard_key, ttl)

    monkeypatch.setattr(RedisStreamQueue, "enqueue_once", flaky)
    return lambda: monkeypatch.setattr(RedisStreamQueue, "enqueue_once", real)


def test_redis_failure_mid_page_is_never_reported_as_success_and_a_rerun_completes(
    redis_client, monkeypatch
):
    _page_size(monkeypatch, 100)
    campaign_id, contact_ids = _campaign(250)
    restore = _fail_enqueue_after(monkeypatch, succeed=137)  # dies 37 jobs into page 2

    response = _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue")

    assert response.status_code == 503  # not a 200, and not a fabricated count
    assert "137" in response.text  # the truthful number of jobs actually queued
    assert len(_queued_keys(redis_client)) == 137
    assert set(_contacts(contact_ids).values()) == {(ContactStatus.PENDING, 0)}  # DB untouched
    with _Session() as s:
        row = s.execute(
            select(AuditLog).where(
                AuditLog.entity_id == campaign_id, AuditLog.action == "campaign.enqueue_failed"
            )
        ).scalar_one()
        assert row.event_metadata["enqueued"] == 137 and row.event_metadata["discovered"] == 137

    restore()  # Redis is back: the same request, restarted from nothing in memory
    rerun = _enqueue(campaign_id)
    assert rerun["enqueued"] == 250 - 137 and rerun["skipped_duplicate"] == 137
    assert rerun["discovered"] == 250 and rerun["complete"]
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


def test_redis_down_from_the_start_queues_nothing_and_says_so(redis_client, monkeypatch):
    campaign_id, contact_ids = _campaign(30)
    _fail_enqueue_after(monkeypatch, succeed=0, exc=redis_lib.TimeoutError("timed out"))
    response = _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue")
    assert response.status_code == 503 and "0 job" in response.text
    assert _queued_keys(redis_client) == []
    assert redis_client.keys("enqueued:*") == []  # no guard may outlive a job that was not queued


def test_a_failing_xadd_never_leaves_a_guard_behind(redis_client):
    """The stranded-guard defect: guard set, then XADD fails => contact blocked for an hour."""
    queue = get_queue()
    redis_client.delete(queue.stream_key)
    redis_client.set(queue.stream_key, "not-a-stream")  # XADD inside the script now errors
    job = DialJob.new(campaign_id=uuid.uuid4(), contact_id=uuid.uuid4(), attempt_number=1)
    with pytest.raises(redis_lib.RedisError):
        queue.enqueue_once(job, enqueue_guard_key(job.idempotency_key), 3600)
    assert redis_client.exists(enqueue_guard_key(job.idempotency_key)) == 0


def test_database_failure_mid_scan_loses_nothing_and_a_rerun_finishes(redis_client, monkeypatch):
    _page_size(monkeypatch, 100)
    campaign_id, contact_ids = _campaign(450)
    real = ContactRepository.eligible_enqueue_page
    calls = {"n": 0}

    def flaky(repo, cid, *, after_id, limit):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OperationalError("SELECT ...", {}, Exception("connection reset"))
        return real(repo, cid, after_id=after_id, limit=limit)

    monkeypatch.setattr(ContactRepository, "eligible_enqueue_page", flaky)
    with pytest.raises(OperationalError):
        _run(campaign_id)
    assert len(_queued_keys(redis_client)) == 200  # exactly the two pages that were handled

    monkeypatch.setattr(ContactRepository, "eligible_enqueue_page", real)
    rerun = _run(campaign_id)
    assert (rerun.enqueued, rerun.skipped_duplicate) == (250, 200)
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


def test_the_per_request_cap_is_reported_and_repeated_calls_finish_the_campaign(
    redis_client, monkeypatch
):
    _page_size(monkeypatch, 50)
    monkeypatch.setattr(enqueue_service, "MAX_ENQUEUE_PER_REQUEST", 120)
    campaign_id, contact_ids = _campaign(300)

    first, second, third = _run(campaign_id), _run(campaign_id), _run(campaign_id)

    assert (first.enqueued, first.complete) == (120, False)  # never a silent stop
    assert (second.enqueued, second.skipped_duplicate, second.complete) == (120, 120, False)
    assert (third.enqueued, third.complete) == (60, True)
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)


# -- 17, 18, 26: CP12-B pause and the kill switch -------------------------------------------------


def test_pause_during_the_scan_keeps_cp12b_semantics_nothing_dials_and_resume_recovers_all(
    redis_client, monkeypatch, provider
):
    _page_size(monkeypatch, 40)
    campaign_id, contact_ids = _campaign(160)

    def pause_after_first_page(page_no, _after):
        if page_no == 2:
            assert _set_status(campaign_id, "paused").status_code == 200

    _PageSpy(monkeypatch, hook=pause_after_first_page)
    result = _run(campaign_id)  # enqueue checks campaign state once, up front (unchanged)
    assert result.enqueued == 160 and _status(campaign_id) == CampaignStatus.PAUSED

    spy = _SpyProvider(provider)
    _drain(spy, redis_client)  # workers read every job and refuse all of them
    assert spy.calls == 0 and _attempts_by_contact(contact_ids) == {}
    assert set(_contacts(contact_ids).values()) == {(ContactStatus.PENDING, 0)}  # all recoverable

    assert _set_status(campaign_id, "active").status_code == 200
    _drain(spy, redis_client)
    assert spy.calls == 160 and set(_attempts_by_contact(contact_ids).values()) == {1}


def test_kill_switch_on_before_enqueue_queues_nothing(redis_client):
    campaign_id, _ = _campaign(10)
    assert kill_switch.enable("test", "cp12c") is True
    assert _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue").status_code == 409
    assert _queued_keys(redis_client) == []


def test_kill_switch_turned_on_mid_scan_changes_nothing_and_workers_do_not_dial(
    redis_client, monkeypatch, provider
):
    """Unchanged semantics: the switch is read when the request starts; workers gate every
    job again before dialing, so jobs queued after it flipped are held, never dialed."""
    _page_size(monkeypatch, 25)
    campaign_id, contact_ids = _campaign(100)
    _PageSpy(
        monkeypatch,
        hook=lambda n, _a: kill_switch.enable("test", "mid-run") if n == 2 else None,
    )
    result = _run(campaign_id)
    assert result.enqueued == 100

    spy = _SpyProvider(provider)
    _drain(spy, redis_client, stop=_stop_after(1.0))
    assert spy.calls == 0

    assert kill_switch.disable() is True
    _drain(spy, redis_client)
    assert spy.calls == 100 and set(_attempts_by_contact(contact_ids).values()) == {1}


def test_no_database_transaction_is_open_while_redis_is_being_called(redis_client, monkeypatch):
    """The read transaction of every page is ended before the first Redis call of that page."""
    _page_size(monkeypatch, 40)
    campaign_id, _ = _campaign(120)
    real = RedisStreamQueue.enqueue_once
    idle_in_transaction: list[int] = []

    def observe(queue, job, guard_key, ttl):
        with _engine.connect() as probe:
            idle_in_transaction.append(
                probe.exec_driver_sql(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state LIKE 'idle in transaction%%' AND datname = current_database()"
                ).scalar_one()
            )
        return real(queue, job, guard_key, ttl)

    monkeypatch.setattr(RedisStreamQueue, "enqueue_once", observe)
    _run(campaign_id)

    assert len(idle_in_transaction) == 120 and set(idle_in_transaction) == {0}


# -- 20: memory -----------------------------------------------------------------------------------


def _peak_bytes(campaign_id) -> int:
    tracemalloc.start()
    try:
        _run(campaign_id)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_peak_memory_does_not_grow_with_campaign_size(redis_client, monkeypatch):
    _page_size(monkeypatch, 100)
    small, _ = _campaign(2_000)
    large, _ = _campaign(10_000)
    _run(_campaign(200)[0])  # warm-up: imports, scripts, connection pool
    small_peak, large_peak = _peak_bytes(small), _peak_bytes(large)
    # 5x the contacts must not cost anything close to 5x the memory: only one page is held.
    assert large_peak < small_peak * 1.5 + 512 * 1024, (small_peak, large_peak)
    assert large_peak < 8 * 1024 * 1024  # and it stays small in absolute terms


# -- 21: configuration ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 500, 5_000])
def test_valid_page_sizes_are_accepted(value):
    assert Settings(_env_file=None, enqueue_page_size=value).enqueue_page_size == value


@pytest.mark.parametrize("value", [0, -1, 5_001, 1_000_000])
def test_invalid_page_sizes_are_rejected_at_startup(value):
    with pytest.raises(ValueError, match="ENQUEUE_PAGE_SIZE"):
        Settings(_env_file=None, enqueue_page_size=value)


def test_default_page_size_is_500():
    assert Settings(_env_file=None).enqueue_page_size == 500


# -- end to end, API contract, CP12-A -------------------------------------------------------------


def test_queued_jobs_reach_workers_and_each_contact_is_dialed_once(
    redis_client, monkeypatch, provider
):
    """PostgreSQL -> keyset pages -> Redis -> worker -> exactly one call each, no lease left."""
    _page_size(monkeypatch, 30)
    campaign_id, contact_ids = _campaign(100)
    _enqueue(campaign_id)

    spy = _SpyProvider(provider)
    _drain(spy, redis_client)

    assert spy.calls == 100 and set(_attempts_by_contact(contact_ids).values()) == {1}
    assert redis_client.zcard(LEASE_GLOBAL) == 0  # CP12-A: enqueue changed nothing about leases
    assert redis_client.keys("concurrency:holder:*") == []


def test_enqueue_itself_touches_no_concurrency_lease_keys(redis_client):
    campaign_id, _ = _campaign(40)
    _enqueue(campaign_id)
    assert redis_client.keys("concurrency:*") == []


def test_response_is_backward_compatible_and_internally_consistent(redis_client, monkeypatch):
    _page_size(monkeypatch, 10)
    campaign_id, _ = _campaign(25)
    body = _enqueue(campaign_id)
    assert {"campaign_id", "enqueued", "skipped_suppressed", "skipped_duplicate"} <= set(body)
    assert (
        body["discovered"]
        == body["enqueued"] + body["skipped_suppressed"] + body["skipped_duplicate"]
    )
    assert body["pages_processed"] == 3 and body["complete"] is True


def test_enqueue_still_requires_an_authenticated_privileged_caller(redis_client):
    campaign_id, _ = _campaign(3)
    assert _api("post", f"/api/v1/campaigns/{campaign_id}/enqueue", role="viewer").status_code in (
        401,
        403,
    )
    assert _queued_keys(redis_client) == []


def test_two_threads_cannot_share_progress_state(redis_client, monkeypatch):
    """No process-local progress exists: two runs interleaved page by page both finish and
    still produce exactly one job per contact."""
    _page_size(monkeypatch, 20)
    campaign_id, contact_ids = _campaign(200)
    gate = threading.Barrier(2)
    seen = {"n": 0}

    def sync(_page_no, _after):
        seen["n"] += 1
        if seen["n"] <= 2:
            gate.wait(timeout=10)

    _PageSpy(monkeypatch, hook=sync)
    results = []

    def run():
        results.append(_run(campaign_id))

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r.enqueued for r in results) == 200
    _assert_queue_is_exactly(redis_client, campaign_id, contact_ids)
