# Checkpoint 12-C — Complete, Paginated Enqueue (H2)

## 1. Problem

Enqueuing a campaign could queue only part of its eligible contacts. The reported case: ~1,200
eligible contacts, ~700 jobs created, HTTP 200.

## 2. Root cause (reproduced)

`QueueEnqueueService.enqueue_campaign` paged with `LIMIT 500 OFFSET n` over
`WHERE status = 'PENDING' ORDER BY created_at DESC, id DESC`, and recomputed `total` on every page
(`if offset >= total: break`).

Dialing workers consume queued jobs *while the scan runs* and move those contacts out of `PENDING`.
The filtered set therefore shrinks under a fixed offset: the next `OFFSET 500` jumps over rows that were
never visited, and the shrinking `total` ends the loop early. (Concurrent inserts shift an `ORDER BY
created_at DESC` window the same way.)

Reproduced on the pre-change code (`e82693f`) with workers "dialing" what was queued between pages:

| eligible contacts | enqueued by the old code |
|---|---|
| 1,200 | **700** |
| 5,000 | **2,500** |

Second defect found: the guard (`SET NX`) was set **before** `XADD`. A failure in between left a guard
with no job, blocking that contact for the guard's lifetime (1 h). Smaller issues: one suppression
`SELECT` per contact (N+1), full `Contact` entities fetched, the read transaction held across every
Redis call, the 100,000 cap ending the scan silently, and a Redis error surfacing as an unhandled 500.

## 3. Existing behavior preserved

Eligibility is unchanged: campaign `ACTIVE`, contact `PENDING` with `attempt_count == 0`, not suppressed,
not already guarded; kill switch checked once when the request starts (409 on, 503 if unreadable);
`campaign.enqueued` audit row; jobs are `attempt_number=1` `DialJob`s with the same JSON and the same
`enqueued:<idempotency_key>` guard key. The worker, CP12-A leases and CP12-B pause/resume are untouched.

## 4. New pagination strategy

Keyset pagination on `contact.id` (`ContactRepository.eligible_enqueue_page`):

```sql
SELECT id, normalized_phone_number FROM contact
WHERE campaign_id = :c AND status = 'Pending' AND attempt_count = 0 AND id > :cursor
ORDER BY id LIMIT :page_size
```

Loop: fetch page → **stop only when the page is empty** (never on a short page) → one set-based
suppression query for the page → `commit()` to end the read transaction → per contact, an atomic
guard+`XADD` → advance the cursor to the last row **only after the whole page was handled** → next page.
Progress lives only in local variables; nothing is held across pages except one bounded page.

## 5. Cursor design

`contact.id` is the primary key: unique, indexed, and totally ordered (PostgreSQL orders `uuid` bytewise,
identical to Python's `UUID` ordering — tested with extreme values). `id > cursor` cannot repeat or skip a
row that stays eligible for the whole run, whatever happens to other rows. Rows that become eligible, or
are inserted, *behind* the cursor during a run are not seen by that run; the next (idempotent) run takes
them.

## 6. Page size

`ENQUEUE_PAGE_SIZE` (default 500). Validated at startup: 1 ≤ value ≤ 5,000, otherwise the application
refuses to start.

## 7. Idempotency

Unchanged mechanism, made atomic: `RedisStreamQueue.enqueue_once` runs one Lua script — if the guard
exists return 0; else `XADD`, then set the guard (1 h TTL), return 1. Add-before-guard means an in-script
failure can never strand a guard (Redis scripts do not roll back); the worst case is a later harmless
duplicate reference. The database constraint `(contact_id, attempt_number)` and the CP12-A per-attempt
lease remain the final protection against a duplicate *call*.

## 8. Concurrent enqueue

Two or more concurrent enqueues of one campaign each scan everything; every contact is won by exactly one
(`EXISTS`+`XADD`+`SET` is atomic), the rest count it as `skipped_duplicate`. No campaign lock exists or is
needed. Tested with 8 concurrent HTTP requests and with two runs forced to interleave page by page.

## 9. Failure behavior

* **Redis error mid-run:** the loop stops, an audit row `campaign.enqueue_failed` (committed first) records
  the true counts, and the request fails with **503** whose message states how many jobs were queued. Jobs
  already queued stay queued and guarded; the cursor and counts reflect only handled contacts; no later
  page is touched. Re-running is idempotent and finishes the rest.
* **Database error mid-run:** the exception propagates (500); nothing is persisted that could be corrupted
  (cursor is local); queued jobs stay; a re-run completes.
* **Per-request cap:** `MAX_ENQUEUE_PER_REQUEST` (100,000, now counting *newly queued* jobs) ends the run
  with `complete=false`. Repeat the call to continue; each call re-scans the already-guarded prefix.
* **Response** (additive, defaulted fields): `discovered`, `pages_processed`, `complete`, alongside the
  existing `enqueued`, `skipped_suppressed`, `skipped_duplicate` ("already queued"). For a finished run
  `discovered == enqueued + skipped_suppressed + skipped_duplicate`. There is no `failed` field: a failure
  is an error response, never a result that could be mistaken for success.

## 10. Redis failure

No in-memory fallback, no fabricated success; see above. An unreadable kill switch still returns 503
before any scan.

## 11. Pause (CP12-B)

Unchanged: campaign status is read from PostgreSQL once at the start (422 if not `ACTIVE`). A pause during
a run does not stop the run; workers drop the paused jobs before admission and resume rebuilds them
(tested end to end: nothing dials while paused, every contact dials exactly once after resume).

## 12. Kill switch

Unchanged: checked once at the start. Switching it on mid-run does not stop the scan; every worker gates
each job again, so nothing dials (tested).

## 13. Database / index considerations

No migration. Existing indexes on `contact`: primary key, `uq_contact_campaign_phone`,
`ix_contact_campaign_status (campaign_id, status)`. For the page query PostgreSQL walks `contact_pkey`
from the cursor (`Index Cond: id > cursor`) and filters, so each page starts where the previous ended
(no per-page rescan; one pass over the id range per enqueue). Measured with `EXPLAIN (ANALYZE, BUFFERS)`:

| table | target campaign | plan | one 500-row page |
|---|---|---|---|
| 222,400 rows (the benchmark DB) | 100,000 contacts | pkey scan | **1.2 ms** (1,173 buffers hit) |
| 3,000,000 rows, 30 campaigns (scratch DB) | 100,000 contacts (1/30) | pkey scan, 18,129 rows filtered | **92 ms** cold (18.7K buffers) |
| same, with a candidate `(campaign_id, status, id)` index | same | index range scan | **1.9 ms** (536 buffers) |

The per-page cost therefore tracks the campaign's share of the *table*, not the campaign size. At 3M rows
a 100K-contact enqueue spends roughly 200 × 92 ms ≈ 18 s of cold database time; the composite index would
make it independent of table size. **It is not required for correctness and was not added** (migrations were
to be avoided unless absolutely required). It is a recommended follow-up if `contact` grows to many
millions of rows across campaigns: `CREATE INDEX ix_contact_campaign_status_id ON contact (campaign_id,
status, id)`. The 3M-row figures come from a scratch database, not from an enqueue run.

## 14. Memory

One page (≤ `ENQUEUE_PAGE_SIZE` rows of `(uuid, str)`) is held at a time; no list of processed ids exists.
Measured Python-level peak (tracemalloc): 0.76 MB at 1,200 contacts, 1.15 MB at 10,000, **1.17 MB at
100,000**.

## 15. Tests

`tests/test_cp12c_enqueue_pagination.py` — 39 tests on real PostgreSQL and Redis: sizes and boundaries
(7, 50, 51, 100, 1,200, 10,000), empty campaign, bounded paging with a spy (5,000 contacts / page 64:
80 queries, none over 64 rows, strictly increasing ids, each row once), the H2 reproduction (workers
consuming mid-scan), inserts mid-scan, short page mid-scan, UUID extremes, mixed eligibility with truthful
counts, repeat and concurrent enqueue, Redis failure mid-page and from the start with audit + exact counts
and a successful re-run, stranded-guard prevention, database failure mid-scan + re-run, cap reporting,
pause and kill switch during a run, worker end-to-end (every contact dialed once, no lease left), no open
DB transaction during Redis calls, flat memory, page-size validation, response contract, RBAC.
Mutation check: 13 injected bugs (`>=` cursor, wrong cursor advance, stop after the first page, skip the
final page, stop on a short page, no guard, guard before add, swallowed Redis error, success after partial
failure, suppression ignored, silent cap, transaction held, double-counted discovery) — all caught.

## 16. Benchmark (this sandbox only: Redis 7.0.15, PostgreSQL 16, Python 3.12, one process, local sockets)

| contacts | pages | enqueued | stream length | SQL statements | duration | Python peak |
|---|---|---|---|---|---|---|
| 1,200 | 3 | 1,200 | 1,200 | 9 | 0.18 s | 0.76 MB |
| 10,000 | 20 | 10,000 | 10,000 | 43 | 1.36 s | 1.15 MB |
| 100,000 | 200 | 100,000 | 100,000 | 403 | 17.3 s (≈5.8K/s) | 1.17 MB |

Process RSS growth during the run: 0.0–0.2 MB. Durations are without tracemalloc (which roughly triples
them). These are not production throughput numbers. Reproduce with
`scripts/bench_enqueue_pagination.py N [--tracemalloc]`.

## 17. Query plan

See section 13. The SQL above is exactly what SQLAlchemy emits (checked by compiling it).

## 18. Known limitations

1. Enqueue still runs synchronously in the request (≈17 s for 100K here); a proxy/client timeout shorter
   than that would cut the HTTP response, not the safety: re-running is idempotent.
2. A re-run after a crash re-scans from the start, and already-guarded contacts cost one Redis call each.
   Guards expire after 1 h; a re-run after that can add a second reference for a contact whose first
   reference is still queued (harmless: one call, enforced by the DB constraint and the CP12-A lease).
3. Contacts that become eligible or are inserted behind the cursor during a run wait for the next run.
4. `MAX_ENQUEUE_PER_REQUEST` makes a campaign above 100,000 eligible contacts need several calls.
5. Page cost grows with table size without the composite index (section 13).
6. Single Redis instance assumed (multi-key Lua), as in CP12-A.
7. Not run against a real Dograh account or a production-sized dataset.

## 19. Rollout

Deploy the API; no migration, no worker change, no queue-format change. Optional: set `ENQUEUE_PAGE_SIZE`.
Clients that ignore unknown JSON fields need no change. A Redis failure during enqueue is now a 503 with a
message and an audit row, where it used to be an unhandled 500.

## 20. Rollback

Revert the commit (files: `enqueue_service.py`, `redis_queue.py`, `contact_repository.py`,
`suppression_repository.py`, `schemas/queue.py`, `config.py`, `.env.example`, the new test, script and this
note). No migration to reverse, nothing to clean in Redis or PostgreSQL; guard keys and `DialJob` JSON are
identical in both versions, so old and new code interoperate on the same queue and API instances can be
rolled one at a time. `ENQUEUE_PAGE_SIZE` is simply ignored by older code.

## Out of scope

H1, H3 sweeper, H4 reclaim/DLQ, H5 reclaim timing, CP13 provider fixes, CP14 compliance, frontend, Dograh.
