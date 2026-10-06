# Checkpoint 12-B — Paused Job Safety (C3)

## 1. The problem (audit of the pre-CP12-B code)

Pause/resume already existed as a plain status change on `Campaign.status` (`ACTIVE`/`PAUSED`, via
`POST /admin/campaigns/{id}/status` and `PATCH /campaigns/{id}`, both through
`CampaignService.update_campaign`). Defects found:

1. **Paused jobs were acked and orphaned.** The only pause check was `DialEligibilityService`, called
   from `_dial`, which returned `NOT_ELIGIBLE` — an outcome the worker **acks**. The Redis message was
   gone, the contact stayed `PENDING`, and the 1-hour enqueue guard (`enqueued:<idempotency_key>`) stayed
   behind, so re-running enqueue after resume skipped it as a "duplicate". Resume itself re-queued nothing.
2. **The check ran after admission**, so a paused job first took a CP12-A lease and a CPS slot.
3. **No row lock on transitions** (`db.get`), so concurrent pause/resume was last-writer-wins; audit rows
   had no `request_id`.
4. Retries were already safe: `recovery/dispatch.py` reschedules a paused campaign's due retries.

No schema change was needed (existing `Campaign.status`); no migration.

## 2. Durable state

PostgreSQL `Campaign.status` is the only pause state. Nothing about pause lives in Redis; there is no
cache. A Redis restart cannot un-pause a campaign.

## 3. Pause semantics

`PAUSED` is committed (one locked row read, one update, one audit insert — **3 statements regardless of
campaign size**, measured at 10 / 1,000 / 100,000 contacts) before the API replies. Pause never touches
contacts, the queue, running calls, attempts, retry state or leases. Repeating a pause is a no-op (logged
`campaign_transition_noop`, no second audit row).

## 4. Worker behavior (`dialer_worker.py`)

```
read job -> kill switch -> [durable campaign status] -> admission (CP12-A lease + CPS) -> _dial
                                    |                                                     |
                                PAUSED                          re-check in _dial (race) -> PAUSED
                                    v                                                     v
                              _settle_paused                          lease released by `finally`, then _settle_paused
```

* `_campaign_is_paused` is a fresh column query of `campaign.status` (not `db.get`: the identity map could
  hold a pre-pause copy). Redis state is never consulted. A paused job takes **no** lease and no CPS slot.
* `_dial` keeps its own check (after the already-processed guard) and returns `CAMPAIGN_PAUSED` instead of
  the acked `NOT_ELIGIBLE`; this covers a pause landing after the early check, and releases the lease
  through the existing CP12-A `finally`.
* `_settle_paused` chooses what happens to the message, without sleeping or re-queuing:
  * **`CAMPAIGN_PAUSED` (acked):** the job is a first attempt (`attempt_number == 1`, no `recovery_type`)
    and its contact is still `PENDING` with `attempt_count == 0` — exactly what `enqueue_campaign` selects,
    so PostgreSQL can rebuild it. Its enqueue guard is deleted, **then** the message is acked (a crash in
    between leaves a pending message, never an unrecoverable job).
  * **`ALREADY_PROCESSED` (acked):** a duplicate delivery of an attempt that already has a provider call.
  * **`CAMPAIGN_PAUSED_HELD` (left unacked):** everything else — retries, recovery jobs, unresolved
    claims. These exist only in Redis/the recovery scheduler, so they are never dropped; the normal
    reclaim pass re-drives them.
* Attempt rows, `attempt_count`, retry schedule and failure reasons are never written for a paused job.
* Event: `campaign_job_blocked_paused` (job_id, trace_id, campaign_id, disposition).

## 5. Resume

`CampaignService` sets `resumed` on PAUSED→ACTIVE; the route **commits first**, then calls
`requeue_after_resume`, which re-runs the existing idempotent `enqueue_campaign`. Contacts with a live
reference keep their guard and are skipped (`SET NX`), so only the references dropped during the pause are
queued again, once. Repeated resumes are no-ops and re-queue nothing. If the re-queue cannot run (global
kill switch on, campaign paused again meanwhile, Redis down) the resume still succeeds and logs
`campaign_resume_requeue_deferred|failed`; contacts stay `PENDING` and unguarded for the next
`POST /campaigns/{id}/enqueue`. Because the same enqueue rebuilds from PostgreSQL, a Redis wipe during a
pause also loses no logical first-attempt job.

## 6. Concurrency / races

* Transitions take `SELECT ... FOR UPDATE` on the campaign row (short transaction, no network call inside;
  Redis is touched only after the commit). Concurrent pause+pause / resume+resume produce one transition;
  pause vs resume serialize, and the audit trail strictly alternates.
* Pause landing between a worker's check and its admission: blocked by `_dial`'s re-check, lease released.
* Two workers with the same job during a pause: both settle it; resume re-queues one reference; CP12-A's
  per-attempt lease and the DB unique constraint guarantee one call.
* An already-placed call is untouched; a duplicate delivery of it resolves as `ALREADY_PROCESSED`.

## 7. Interactions

* **Retries:** held, never dropped, no count change, no failure recorded; they dial once after resume.
* **Kill switch:** unchanged and still checked first; kill switch ON + resume ⇒ campaign `ACTIVE`, no call.
* **CP12-A:** paused jobs never reach `try_admit`; a lease taken before a detected pause is released via
  the existing `finally`. No lease/counter code was changed.
* **Redis failure:** pause needs only PostgreSQL. A Redis error while settling a paused job propagates and
  leaves the message pending. There is no in-memory fallback.
* **Auth/audit (CP11):** routes, RBAC (`admin` mutates; `operator`/`viewer` refused) and rate limits are
  unchanged. Audit rows (`campaign.status_changed`) now also carry `request_id` and `result`. Events:
  `campaign_paused`, `campaign_resume_requested`, `campaign_resumed`, `campaign_transition_noop`,
  `campaign_pause_transition_rejected`, `campaign_resume_transition_rejected`. No metrics framework exists,
  so none were added.

## 8. Rollout

No migration, no new setting. Deploy API and workers together; a worker without this change would still
ack paused jobs (the old behavior). Contacts stranded by the old behavior (PENDING, unattempted, guard
still set) are recovered by re-running enqueue once their guards expire (≤ 1 h), or by deleting those
`enqueued:*` keys.

## 9. Known limitations (verified)

1. **Residual window:** a pause committed after `_dial` reads the status but before the Dograh request
   completes cannot stop that one call (a check and an HTTP call cannot be atomic without holding a lock
   across the network, which is forbidden). Same width as the kill switch.
2. Resume re-queues **synchronously inside the request** and scans all `PENDING` contacts (existing
   `enqueue_campaign`: 500-row offset pages, 100,000 cap — this is H2). Its latency scales with the
   backlog; it was not benchmarked at 100K.
3. A failed/deferred re-queue is not retried automatically (no sweeper — H3); the operator re-runs enqueue.
4. If a reference has been live in the stream for more than the 1-hour guard TTL, a resume's re-queue can
   add a second reference. It is harmless (one call), but the "no duplicate entries" property is only
   guaranteed within the guard TTL. Likewise a crash between guard delete and ack can leave one extra
   reference.
5. Held retry/recovery messages are re-driven at the reclaim cadence (H5), so many of them resume slowly.
6. One extra column query per job on the worker hot path (not benchmarked).

## 10. Out of scope

H2 enqueue pagination, H3 sweeper, H4 reclaim/DLQ, H5 reclaim timing, DB retry scheduling, CP13 Dograh
fixes, CP14 compliance, CP14B intelligence, CP15 recording/backup, CP16 infrastructure, frontend.
