# Checkpoint 01A — database audit

Audit performed against the schema as merged from Checkpoint 01,
inspected directly in PostgreSQL (`\d`, `pg_constraint`,
`enum_range()`) rather than only read from model/migration source.

## Findings and fixes

1. **Timestamps were not UTC-aware (Step 17 — real bug).** Every
   `DateTime` column (`created_at`, `updated_at`, `started_at`,
   `ended_at`, `requested_at`, `received_at`, `occurred_at`) mapped to
   Postgres `timestamp without time zone`, not `timestamptz`. A naive
   timestamp is ambiguous the moment the app or database server's
   local timezone isn't UTC. Fixed: every timestamp column now uses
   `DateTime(timezone=True)`; migration alters existing columns
   `USING column AT TIME ZONE 'UTC'` (safe here since the columns are
   currently empty in every real environment — Checkpoint 01 shipped
   without ever going to production — but written as a real `ALTER
   COLUMN ... TYPE timestamptz` either way, not a drop/recreate).

2. **`call_event.call_attempt_id` had no index (Step 18 — "call events
   by call").** Postgres does not automatically index foreign-key
   columns (only the referenced primary key). Added.

3. **`audit_log` had no index supporting "by entity" or "by time"
   (Step 18 — "audit logs by entity/time").** Added a composite index
   on `(entity_type, entity_id)` for "history of this record" lookups,
   and a separate index on `created_at` for time-range/recent-activity
   queries.

4. **`campaign.status` had no index (Step 18 names "campaigns by
   ... status" explicitly).** Added — cheap, single-column, directly
   named in the checkpoint's own expected access patterns.

## Verified correct, no change needed

- **Enum values** — re-checked directly against Postgres with
  `enum_range()`, not just read from model code. All ten enums store
  the canonical spec string (`Pending`, `no_answer`, `granted`, ...),
  not the Python member name.
- **No cascading deletes anywhere** — checked `pg_constraint.confdeltype`
  for all 7 foreign keys directly; every one is `a` (`NO ACTION`).
  Historical call/audit/compliance records cannot disappear because a
  parent row was deleted.
- **Call-attempt idempotency** — `UNIQUE(contact_id, attempt_number)`.
  This checkpoint's Step 6 frames the invariant as `campaign_id +
  contact_id + attempt_number`, but a contact belongs to exactly one
  campaign (direct FK, no join table — see
  `docs/CHECKPOINT-01-NOTES.md`), so `contact_id` already uniquely
  determines `campaign_id`; the two-column constraint enforces the
  same invariant without a redundant denormalized `campaign_id` column
  that the reconciled `Database-Design.md` schema for `call_attempt`
  doesn't have.
- **Processed-event idempotency** — `UNIQUE(event_id)`, confirmed
  rejects a duplicate at the database level (test:
  `test_duplicate_event_id_is_rejected_at_db_level`).
- **Retry policy invariant** — the
  `jsonb_array_length(retry_spacing_seconds) = max_retries` CHECK
  constraint, confirmed present in `\d retry_policy`.
- **Suppression** — still the single canonical table; no
  `dnc_records`/`consents` duplicate was introduced.
- **Tenant isolation (Step 15)** — the reconciled `Database-Design.md`
  has no tenant concept anywhere (re-checked: no `tenant_id` in any
  table definition), so there is nothing to add here. Documented in
  Checkpoint 01 and re-confirmed rather than silently re-litigated.
- **Session management** — one engine created at import time
  (`app/core/database.py`), a new session per request via the
  `get_db` FastAPI dependency, closed in a `finally` block, rollback
  on exception. No global mutable session, no per-request engine.
- **`SQLite`** — not used anywhere; `PRIMARY_DB_URL` is Postgres-only
  and tests run against a real `ai_calling_agent_test` database.

## Step 26 — index sanity check (EXPLAIN)

Seeded 500 campaigns (mixed status) and 50,000 contacts (mixed status,
spread across campaigns) to get realistic selectivity, then ran
`EXPLAIN ANALYZE` (all seed data truncated afterward):

- `contact` lookup by `(campaign_id, normalized_phone_number)` (the
  dedup-on-import check) → **Index Scan on
  `uq_contact_campaign_phone`**.
- `contact` filtered by `(campaign_id, status)` (the queue-dispatch
  pattern) → **Bitmap Index Scan on `ix_contact_campaign_status`**
  once the data had realistic variety (an earlier pass with all rows
  sharing one campaign/status showed a seq scan, which was the correct
  planner choice for that degenerate case, not a missing-index problem
  — redone with mixed data to get a meaningful answer).
- `campaign` filtered by `status` → sequential scan, and that's
  correct: 500 rows fits in a handful of pages, so Postgres rightly
  skips the index at this size. It'll pick up `ix_campaign_status`
  automatically once the table is large enough for an index to help —
  no action needed now, per Step 26's "do not optimize prematurely."
