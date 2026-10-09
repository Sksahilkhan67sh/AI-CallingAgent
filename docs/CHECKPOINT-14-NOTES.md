# Checkpoint 14 — Compliance basics + spend cap

Branch `feature/checkpoint-14-compliance-spend-cap`, from `develop` @ `27997f5` (the CP13 merge).
Labels used on every statement: **VERIFIED** (proven by a test on real PostgreSQL/Redis, a
mutation, or by reading the code), **UNVERIFIED**, **BLOCKED**, **DOCUMENTED LIMITATION**,
**DEFERRED**, **PRODUCT DECISION** (a default the owner must confirm).

## 1. Objective and scope

Make the system safe to point at real phone numbers: one strict phone normalizer (C4), calling
windows read in the campaign's timezone (C5a), a retry policy every campaign always has (C5b),
a global do-not-call list that can hold numbers without a contact, and a basic daily dial /
estimated-spend cap. Single tenant: no tenant columns were added.

Out of scope and untouched (adjacent findings are in §10): stuck-call sweeper, reclaim/DLQ/stream
trim, reclaim timing, timeouts, DB-driven retry schedule, provider config-error handling, the four
open CP11 gaps, analysis/H8, recording/backup, infra/logging, load testing, per-campaign and
monthly budgets, PII masking, tenant isolation, consent management, registry integration.

## 2. Preconditions and baseline

- **VERIFIED** `docs/CHECKPOINT-12A/12B/12C/13-NOTES.md` exist and their code is on `develop`
  (`git merge-base --is-ancestor` for each).
- Baseline before any change: `ruff` clean, `mypy app` clean (141 files), one alembic head
  `d15c334bf7e7`, `alembic check` clean, **1137 passed**.
- **VERIFIED** The reproductions in §3 were written first and all 13 FAILED on the baseline
  (commit `60ce3f9`). They were removed once superseded by the CP14 suites; the failing output is
  reproduced in §3.
- **BLOCKED** Dograh credentials / live access (as in CP13). Nothing here needs Dograh to be
  reachable; no live call was made.
- Test environment: PostgreSQL 16 and Redis installed locally; Python **3.12.3** (the image is
  `python:3.11-slim`). **UNVERIFIED** on Python 3.11 — nothing used is version-specific, but it
  was not run there.

## 3. Verified problems → failing reproduction → fix

| # | Problem (baseline behaviour) | Failing reproduction on `27997f5` | Fix | Label |
|---|---|---|---|---|
| C4 | `9876543210` → `+19876543210` (US); `098765 43210` → `+09876543210`; 16-digit numbers accepted | `+19876543210 != +919876543210`; `+09876543210 != +919876543210`; `DID NOT RAISE` | `app/services/phone.py` on `phonenumbers` | VERIFIED |
| C5a | window compared to UTC (`10:00-18:00` meant 15:30-23:30 IST); overnight never matched | `eligible is False` at 10:00 IST; `True` at 18:30 IST; overnight `False` | `app/services/calling_window.py`, used by every call path | VERIFIED |
| C5a′ | first-attempt job outside its window → `NOT_ELIGIBLE` → **acked and lost** (contact stays `PENDING`, job exists nowhere) | `job lost (outcome=not_eligible)` | deferred to the recovery scheduler, CP12-B ordering | VERIFIED |
| C5b | campaign without a `RetryPolicy` row: no retries **and** 24x7 calling; nothing creates the row | `0 rows == 1`; `eligible is True` at 03:00 IST | policy born with the campaign; `get_effective_policy` | VERIFIED |
| DNC | PK was `contact_id`, so a number with no contact could not be stored; no API | null `contact_id` rejected; `POST` → 404 | suppression restructured; admin API | VERIFIED |
| Spend | nothing limited calls per day; call duration not persisted | job dialed with `DAILY_DIAL_CAP=0`; no `duration_seconds` | `spend_cap`, `outbound_gate`, `call_attempt.duration_seconds` | VERIFIED |

Baseline run: `13 failed` (`tests/test_cp14_repro.py`, commit `60ce3f9`).

## 4. What changed

### 4.1 Phone normalization (C4) — VERIFIED
`normalize_phone(raw, default_region, allowed_regions=None) -> NormalizedPhone | InvalidPhoneError(code)`.
Strict E.164 (≤15 digits) and `is_valid_number`. Codes: `empty`, `invalid_number`, `too_long`,
`region_not_allowed`. Only the typed error escapes (3,000-input fuzz + non-string inputs).

`phonenumbers` silently **accepts extensions** (`x12`, `ext 5`) and **converts vanity letters**
(`1-800-FLOWERS`). Both would dial a different number than typed, so the character set is validated
before the library is called. **VERIFIED** (parametrized tests + mutation M8).

Equivalence classes: `9876543210`, `09876543210`, `919876543210`, `+91 98765 43210`,
`0091 98765 43210`, full-width digits … all give `+919876543210`.

Call sites (every one goes through `normalize_phone`; grep found no ad-hoc normalization left):

| Path | Where |
|---|---|
| contact create / update | `ContactService._validate_and_normalize` (campaign region + allowed regions) |
| CSV import (per-row codes) | `ContactImportService` |
| association / reassignment | `CampaignContactService` (re-validates against the TARGET campaign) |
| suppression add / import | `SuppressionService` |
| CP13 Dograh opt-out writer | `_suppress_for_opt_out` → `SuppressionRepository.insert_if_absent` |
| native (AI) opt-out writer | `ConversationOrchestrator` → `insert_if_absent` |
| eligibility + all suppression lookups | `SuppressionRepository.canonical_number` (`is_suppressed`, `suppressed_among`, `get_by_phone`, `list`) |
| CP12-C enqueue suppression page query | `suppressed_among` |
| dial-time region re-check (legacy rows) | `DialEligibilityService` → `is_dialable_region` |

`ContactRepository.campaign_counts` still reads `Suppression.phone_number` directly in one SQL
subselect. It is a **reporting count, not a dial decision**, and cannot call the Python normalizer;
it relies on stored values being canonical, which the backfill (§8) guarantees. **DOCUMENTED LIMITATION**.

`region_not_allowed` is enforced at write time (contacts, CSV, association) and re-checked at dial
time. It is **not** applied to suppression writes: refusing to record a foreign do-not-call number
would leave it callable if the allowed regions ever widen. **PRODUCT DECISION**.

Schemas: `Campaign.timezone` / `default_region` (validated: `ZoneInfo` must load; region must be an
ISO country code, normalized upper-case). `default_region` is **not editable after creation**
(existing contacts were parsed with it). **PRODUCT DECISION**.

### 4.2 Calling window (C5a) — VERIFIED
`is_within_calling_window(now_utc, tz, start, end)` is pure; `now` is injected. Half-open
`[start, end)`, overnight supported, `start == end` rejected, naive datetimes refused, IANA zones
via `zoneinfo`, no fixed-offset fallback. Hard bound (`HARD_CALLING_WINDOW_*`, default 09:00-21:00
in the campaign's zone): rejected on save **and** clamped at evaluation (both windows must be open).

**No call path compares against UTC.** Proven two ways: a static AST test (the only `.time()` call in
`app/` is in `calling_window.py`; policy window columns are read only by the policy service) and
behavioral tests per path. Mutations M1 (tz ignored) and M2 (clamp removed) fail tests.

Outside-window handling (the dialer, before admission so a backlog does not burn CPS/concurrency):

| Job | Disposition |
|---|---|
| first attempt, contact still `PENDING`, never attempted | scheduler entry (due at next opening) **first**, enqueue guard released **second**, stream ack **third** → `WINDOW_DEFERRED`; PostgreSQL can always rebuild it |
| retry arriving after the window closed | rescheduled as a normal `RecoveryJob` → `WINDOW_DEFERRED` |
| anything else (no recovery context; corrupt timezone; window that never overlaps the bound) | left **unacked** → `WINDOW_HELD`; re-driven by reclaim; never dials on a guess |

Tests with an injected clock: nothing dialed outside the window, nothing lost, everything dials
exactly once after it opens (12-job backlog), duplicate deferral is one scheduler entry, a failing
scheduler write leaves the message pending and the guard untouched, a number suppressed while
deferred is never dialed, a paused campaign keeps its deferred job. Mutation M3 (ack without
scheduling) fails.

**Time zones verified here:** `Asia/Kolkata`, `Australia/Sydney`, `America/New_York` (DST both
directions, 23-hour budget day).
**BLOCKED** Verifying `ZoneInfo("Asia/Kolkata")` loads **inside `python:3.11-slim`**: the sandbox has
no Docker and the registry returns HTTP 403. It loads on the test host. Mitigation: a timezone that
cannot load is a **boot error** (`Settings` validation) and a per-job hold (never a UTC fallback).
Owner check (one line): `docker run --rm python:3.11-slim python -c "from zoneinfo import ZoneInfo; ZoneInfo('Asia/Kolkata')"`.
If it fails, tzdata must be added — that needs your approval (not added).

### 4.3 Retry policy (C5b) — VERIFIED
`RetryPolicy` model/shape unchanged. `create_campaign` and CSV import create the row in the same
transaction (`ensure_policy`, idempotent, race-safe). `get_effective_policy(db, campaign)` returns
the DEFAULT policy for a legacy campaign and is used by dial eligibility, the dialer, recovery
dispatch and `RecoveryManager`. A missing row can no longer mean "no window, no retries".
`app.scripts.backfill_retry_policies` creates missing rows (dry run by default; not required for
safety, only so the stored state matches what is in force).

API (`/api/v1/admin/campaigns/{id}/retry-policy`): `GET` any authenticated role (`persisted:false`
means the default is in force); `PUT` admin only, `MUTATION_LIMIT` budget, audit
`retry_policy.updated` with actor and before/after (VERIFIED: authz matrix 401/403/200, 13 validation
cases each leaving the stored row unchanged, 429 on budget).

### 4.4 Do-not-call (global) — VERIFIED
Schema: `suppression` PK `id`; `contact_id` NULLABLE (FK kept, unique when present);
`phone_number` UNIQUE globally; `created_by`. No enum change (`manual_api` and `agent_in_call` exist).
Both CP13/native opt-out writers conflict on the **number** (`ON CONFLICT DO NOTHING`, any unique
violation), keep audit-only-by-the-inserter, and all CP13 tests pass on the new schema.

Admin API `/api/v1/admin/suppressions`: `POST` add (idempotent: existing number → 200 + existing row,
no second audit), `POST /import` (CP11 caps: 5 MiB, 10,000 rows, import budget; counts added /
already_present / invalid + per-row codes), `GET` list/search (limit ≤ 200, search by any spelling),
`DELETE /{id}` (mandatory reason ≥3 chars, admin, audited). Operators read only; anonymous 401.
Responses return a **masked** number plus last four digits.

Audit metadata = keyed fingerprint + last four digits + reason, never the number (VERIFIED by test on
add/remove; log-capture test over add/import/search/remove finds no digits). The fingerprint is
HMAC-SHA256 keyed from `JWT_SIGNING_KEY` (a plain hash of a 10-digit space is reversible), so rotating
that key changes fingerprints (audit correlation only). **PRODUCT DECISION**.

Dial-time check stays authoritative and uses the same lookup everywhere. A number added **after** its
job was queued is not dialed; the job is acked as not-eligible (permanent), no attempt, no retry budget,
nothing scheduled (VERIFIED). Mutation M6 (lookup not canonicalized) is caught.

Adding a number does **not** close existing contacts that hold it; the dial-time check decides.
Removing a number re-enables calling for future dials; contacts already `CLOSED` by an opt-out stay closed.

**BLOCKED/DEFERRED** Registry scrubbing (NDNC/DND) and consent management: no registry credentials
exist. Bulk import is the supported path for a registry-scrubbed list.

### 4.5 Daily spend cap — VERIFIED (with the limits in §9)
`outbound_gate.block_reason` = kill switch, then budget. It runs (a) before reading the stream,
(b) before admission on a claimed job, (c) in `_dial` before the durable attempt claim. When blocked:
jobs stay queued and unread (xlen intact, pending 0), nothing acked, no admission slot, no attempt, no
retry budget; outcome `BUDGET_BLOCKED`; the kill switch keeps its own `OUTBOUND_BLOCKED` outcome and is
checked first.

Counted **from PostgreSQL** (`call_attempt.started_at` inside the `BUDGET_TIMEZONE` day, new index).
Estimated spend uses stored `duration_seconds` (**new column, populated from the Dograh webhook**,
parsed defensively: non-numeric/negative/non-finite → unknown, capped at 24 h); unknown durations count
`ESTIMATED_MINUTES_PER_UNKNOWN_ATTEMPT`. `0` blocks all dialing. Resumes by itself at the next budget
day (fake clock). **Fails closed** on a database error. Audit `spend_cap.reached`: once per day per cap
(Redis `SET NX`, written in its own session); log: one line per minute.

`GET /api/v1/admin/spend` (any role, read-only): today's dials, estimated minutes/spend, caps, percent used.

## 5. Settings (all in `.env.example`, which a test loads as valid `Settings`)

| Setting | Default | Notes |
|---|---|---|
| `DEFAULT_REGION` / `DEFAULT_TIMEZONE` | `IN` / `Asia/Kolkata` | must load; region must be in `ALLOWED_DIAL_REGIONS` |
| `ALLOWED_DIAL_REGIONS` | `["IN"]` (JSON list) | dial protection |
| `HARD_CALLING_WINDOW_START/END` | `09:00` / `21:00` | compliance backstop, campaign-local |
| `DEFAULT_CALLING_WINDOW_START/END` | `09:00` / `21:00` | new campaigns and legacy rows |
| `DEFAULT_MAX_RETRIES` | `2` | **PRODUCT DECISION** |
| `DEFAULT_RETRY_SPACING_SECONDS` | `[3600, 14400]` | 1 h, 4 h — **PRODUCT DECISION** (old 30 s / 10 min were dev values) |
| `MAX_RETRIES_CEILING` / `MIN_RETRY_BACKOFF_SECONDS` / `MAX_RETRY_BACKOFF_SECONDS` | `5` / `300` / `604800` | API bounds |
| `DAILY_DIAL_CAP` | `1000` | **PRODUCT DECISION**; `0` blocks all |
| `DAILY_ESTIMATED_SPEND_CAP` + `ESTIMATED_COST_PER_MINUTE` | unset | set both or neither (startup error) |
| `ESTIMATED_MINUTES_PER_UNKNOWN_ATTEMPT` | `1.0` | |
| `BUDGET_TIMEZONE` | `Asia/Kolkata` | day boundary |
| `BUDGET_CHECK_CACHE_TTL_SECONDS` | `2` | see §9 |

Validation is enforced always (not only in production): non-negative values, loadable zones, valid
region codes, hard-bound ordering, retry defaults inside their own bounds.

## 6. PRODUCT DECISIONS awaiting the owner

1. **Behaviour change on deploy:** legacy campaigns were callable 24x7 with no retries; they now call
   only 09:00-21:00 IST and retry twice (1 h, 4 h). Confirm or change the defaults.
2. **`DAILY_DIAL_CAP=1000`** will stop any larger run at 1000 calls per IST day. Set it deliberately
   before the first big campaign.
3. Hard bound and default window 09:00-21:00 (campaign-local). **Not legal advice; legal review is
   DEFERRED and owner-owned.**
4. Retry defaults (2 retries; 1 h and 4 h), backoff floor 5 min, ceiling 5 retries.
5. India-only dialing; foreign numbers may still be *suppressed*.
6. `default_region` immutable after campaign creation.
7. Fingerprint keyed from the JWT signing key.
8. Rows whose number is unparseable are still matched verbatim in suppression (never dropped).

## 7. Tests

`219` new tests (+ the existing suite; **1357 total**) in `tests/test_cp14_*.py` (phone 57, window 32, policy 40, suppression 28, spend 41,
renormalize 15, migrations 6) on real PostgreSQL + Redis with an injected clock where time matters.

Mutations run against production code, each of which fails a CP14 test (then restored): M1 window read
in UTC, M2 hard bound not clamped, M3 deferred job acked without scheduling, M4 budget fails open on
DB error, M5 final `_dial` gate removed, M6 suppression lookup not canonicalized (**first survived →
test added → now killed**), M7 legacy campaign without row unrestricted, M8 extensions accepted, M9
dial-time region check removed. **VERIFIED**

Existing-suite changes (listed because the task says to say why):
- **55 existing test files touched in total** (51 fixture rewrites plus the assertion/fixture changes below): fictional `555-…` / `+1555…` / `+1415…` fixture numbers rewritten to valid Indian
  mobiles (`989-…`, `+91989…`; every other digit unchanged). Required: the strict normalizer correctly
  rejects the old ones. 25,000 sampled rewritten-shape numbers all pass `is_valid_number`. Test-side
  `normalize_phone_number` helper in `tests/phone_helpers.py`.
- `tests/conftest.py`: suite-wide env makes the window "always open" and the cap "effectively
  uncapped", plus an autouse reset of the in-process budget cache. Required: ~1100 tests dial at
  whatever hour they run and a missing policy row is now judged by the default window. Every CP14 test
  that exercises the window or cap sets its own values.
- Two generators fixed because they never made valid numbers: `989-71NN-0001` was **11 digits**
  (cp11 kill-switch test); cp12c bulk-inserted synthetic `+000…` numbers.
- **Assertions changed (all mandated by this spec):**
  `test_recovery_manager::test_no_retry_policy_configured_defaults_to_no_retry` asserted the old
  behaviour (missing row ⇒ no retry) → rewritten as "missing row ⇒ default policy" plus a companion
  asserting an explicit zero-retry policy; three flow tests (`test_dialer_worker`, `test_dograh_webhook`,
  `test_ai_disconnect_recovery`) that only reached a terminal state *because* of the missing row now
  state an explicit zero-retry policy (assertions unchanged);
  `test_reconciliation_retry_gate` outcome `NOT_ELIGIBLE` → `WINDOW_HELD` (the old outcome was the
  ack-and-lose bug; the rest of the test is unchanged); `test_bulk_import` reason
  `invalid_phone_number` → `invalid_number`.

## 8. Deploy steps

1. **Before** migrating, check for duplicate suppression numbers (the migration refuses if any exist
   and changes nothing): `python -m app.scripts.dedupe_suppressions` (dry run), review,
   `--apply` (keeps the earliest row per number).
2. `alembic upgrade head` (two schema-only migrations: `c14a0a1b2c3d`, `c14b4e5f6a7b`).
3. `python -m app.scripts.renormalize_phones` (**dry run**) — review changed / unchanged / invalid /
   collisions. Collisions are listed with ids and masked numbers and are never merged or deleted.
4. `python -m app.scripts.renormalize_phones --apply`; re-run: everything must report unchanged.
5. `python -m app.scripts.backfill_retry_policies --apply` (optional).
6. Set `DAILY_DIAL_CAP` (and optionally the spend cap + cost per minute), confirm `DEFAULT_TIMEZONE`,
   `BUDGET_TIMEZONE`, `ALLOWED_DIAL_REGIONS`; confirm tzdata in the image (§4.2).
7. Deploy the code. Until step 4 is applied, legacy rows whose stored number is wrong (e.g.
   `+19876543210`) are **not dialed** — the dial-time region check blocks them (safe direction).

## 9. Known limitations

- **DOCUMENTED LIMITATION** The cap is **not exact** and is an **estimate, not billing**; the
  provider-side limit in Dograh is the real backstop. The check and the attempt claim are not atomic and
  the verdict is cached `BUDGET_CHECK_CACHE_TTL_SECONDS`. Overshoot ≤ dials *started* within one TTL
  (≤ global CPS limit × TTL) + dials in flight at once (≤ global concurrency limit). Both are asserted:
  with no cache, 8 concurrent workers overshot ≤ 8; with the cache never expiring, all queued jobs dial.
- **DOCUMENTED LIMITATION** The recovery scheduler is Redis-based and **not durable**. A deferred
  first-attempt job lives in Redis; if Redis loses it, the contact is still `PENDING` in PostgreSQL with
  the enqueue guard released, so re-running enqueue rebuilds it. Nothing rebuilds it automatically.
- **DOCUMENTED LIMITATION** A legacy row blocked by `region_not_allowed` is acked as not-eligible and
  stays `PENDING` until the backfill fixes its number.
- **DOCUMENTED LIMITATION** Jobs held (`WINDOW_HELD`) wait for the normal reclaim, which is slow (H5).
- **DEFERRED** Legal review of the windows; registry (NDNC/DND) scrubbing; consent management.
- **UNVERIFIED** Python 3.11; tzdata in `python:3.11-slim` (**BLOCKED**); any live Dograh behaviour.

## 10. Adjacent findings (not fixed)

- `RecoveryDispatch` removes a job from the scheduler *then* re-adds it on reschedule; a crash between
  loses it (scheduler durability, H-series).
- `RecoveryManager._decide`'s `no_retry_policy_configured` branch is now unreachable (a policy is
  always supplied); left in place.
- Dispatch moves ≤50 due jobs per pass every 10 loops; fine at current CPS, worth revisiting at CP17.
- `ContactRepository.campaign_counts` (see §4.1).
- Small scope addition: CSV row numbers now use the physical line (`DictReader` skips blank lines, so
  reported rows drifted) — in both CSV readers, with a test.

## 11. Rollback

- Code: revert the PR. Migrations are schema-only and reversible: `alembic downgrade d15c334bf7e7`.
  **Downgrade refuses** (and changes nothing) while any suppression row has no contact, because the old
  primary key cannot hold it — remove or link those rows first. (VERIFIED on a populated database.)
- Rewritten phone numbers are **not** automatically reverted; keep the dry-run output from step 3 and a
  database backup before `--apply`.
- Reverting does not undo `call_attempt.duration_seconds` data (the column is dropped by downgrade).

## 12. Acceptance checklist

- [ ] Owner confirmed §6 decisions (especially 1 and 2).
- [ ] `docker run … ZoneInfo('Asia/Kolkata')` passes in the production image.
- [ ] Dedupe → migrate → renormalize dry run reviewed → apply → second run all unchanged.
- [ ] `DAILY_DIAL_CAP` set for the intended campaign size.
- [ ] A test call outside 09:00-21:00 IST is deferred, and dials after 09:00 (needs Dograh; **BLOCKED** here).
- [ ] Legal review scheduled.

## 13. Gate results (measured on this branch)

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `mypy app` | Success: no issues found in 154 source files (baseline 141) |
| `alembic heads` / `upgrade head` / `check` | single head `c14b4e5f6a7b`; upgrade OK; "No new upgrade operations detected" |
| `pytest` (real PostgreSQL 16 + Redis) | **1357 passed** (baseline 1137), run twice consecutively |

The suite also passes in both calling-window regimes: it ran in daytime and at 23:37 IST. A first
full run exposed two of *my own* tests depending on the hour (a `monkeypatch.undo()` that also undid
the fake clock, and a real-clock test using the default 09:00-21:00 window); both were fixed to be
wall-clock independent. Environment: Python 3.12.3 (image is 3.11, UNVERIFIED), PostgreSQL 16, Redis.
