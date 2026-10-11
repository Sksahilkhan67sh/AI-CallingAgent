# Checkpoint 15A -- Data inventory, retention, legal hold, erasure/export

Labels: **VERIFIED** (proven by a test or an executed run on real Postgres/Redis),
**UNVERIFIED**, **BLOCKED**, **DOCUMENTED LIMITATION**, **DEFERRED**.

Branch `feature/checkpoint-15a-data-lifecycle`, built on `feature/checkpoint-15-data-recording-backup`
(5 docs/hygiene commits, unchanged and not rebased) on top of `develop`.

## 1. Objective

Make personal data inventoried, bounded in time and erasable -- without risking the
do-not-call list -- before any recording or backup work lands.

## 2. Scope split (owner decision)

| Part | Content | Status |
|---|---|---|
| **CP15A (this PR)** | data inventory + classification guard, retention engine, legal hold, erasure/export + fingerprint log, `reapply_erasures`, config validators | delivered |
| CP15B | backup / PITR / restore runbook, scripts, secondary DNC copy, executed drills | not started |
| CP15C | recording ingestion + access API | **BLOCKED**: provider-side consent (decision 9, `CP15-ARCHITECTURE.md` §9) is open. No `call_recording` schema, worker or endpoint was created; `campaign.recording_enabled` was NOT added; the webhook still never persists a recording URL (commit 8b267e9). `boto3` not approved: S3 backend BLOCKED. |

## 3. Baseline (recorded BEFORE any change; sandbox Python 3.12, Postgres 16, Redis 7)

| | ruff | mypy | alembic heads | pytest |
|---|---|---|---|---|
| `develop` @ 5d0b21e | clean | clean (159 files) | `a83d13f0c415` | **1483 passed** |
| CP15 branch (docs + webhook fix) | clean | clean | `a83d13f0c415` | **1506 passed** |

CP14B is merged on `develop` (PR #48), so the analysis columns are classified through the registry.
CP12D (sweeper/reclaim) items were not touched.

## 4. What was built

1. **Data inventory + guard** -- `app/core/data_inventory.py` classifies every column (PII_DIRECT / PII_CONTENT /
   PII_INDIRECT / SECRET / NON_PII) with a retention class. A test fails on an unclassified column, a stale entry,
   or a PII_DIRECT/CONTENT column without a purge (or never-purge) class. `docs/DATA-INVENTORY.md` embeds the
   generated table; a test fails if it goes stale. **VERIFIED** (incl. a failing case for a new column on `call_analysis`).
2. **Schema** (`c15a1b2c3d4e`, one reversible migration): `campaign.legal_hold`, `campaign.closed_at`,
   `contact.legal_hold`, `contact.purged_at`, `erasure_log` (append-only by trigger), `retention_run`, indexes on
   `conversation_message.created_at`, `processed_event.received_at`, `working_memory_snapshot.snapshotted_at`
   (others already existed). **VERIFIED**: upgrade/downgrade/`alembic check` on a database already holding rows;
   downgrade refuses to destroy erasure evidence or lift a hold.
3. **Retention engine** (`python -m app.scripts.run_retention`, dry-run default, `--apply`) -- per-class settings,
   batches, runtime budget, advisory lock, `retention_run` row + counts-only audit event, idempotent/resumable.
   **VERIFIED**: boundary tests per class, legal hold blocks every class, dry run changes no data, apply idempotent,
   batch limit + resume, lock prevents overlap (two real connections), a mid-run failure is sanitized and a re-run
   repairs, audit/run rows contain counts only. Also run as a real process against a fresh DB (dry run, apply, re-apply).
4. **Suppression is never touched** -- the purge module neither imports nor names the table (AST guard test),
   no lifecycle module contains delete/update on it, and a behavioural test runs every class with all periods at 1
   day over a 9999-day-old suppression row. **VERIFIED** (mutation check: importing `Suppression` into the module
   fails the guard).
5. **Legal hold** -- admin-only `PUT /api/v1/admin/data/legal-hold/{campaigns|contacts}/{id}` with a required
   reason; audited. Covers contacts, transcripts, snapshots, analysis, and audit rows about the held entity. **VERIFIED**
   (mutation check: removing the hold filter from the analysis scrub fails 2 tests).
6. **Erasure** -- `POST /api/v1/admin/data/erasure` and `python -m app.scripts.erase_data`: all contacts sharing a
   number across campaigns, suppression added by default, HMAC-SHA256 fingerprint in `erasure_log`, hold refuses the
   whole request, idempotent, audit has fingerprint prefix + last four digits only. **VERIFIED**.
   Scrubbed contacts are CLOSED if still live, and `DialEligibilityService` now refuses a contact with `purged_at`
   (a placeholder number can never be dialed). **VERIFIED**.
7. **Export** -- `GET /api/v1/admin/data/contacts/{id}/export`: bounded (200 attempts / 5000 turns, `truncated` flag),
   audited with counts only. **VERIFIED**.
8. **`reapply_erasures`** -- after a restore, recognises erased numbers by fingerprint and re-scrubs them; skips held
   records and counts them; reports erased fingerprints missing from the DNC list. Dry-run default. **VERIFIED**
   against a simulated restore from an older state.
9. **Status endpoint** `GET /api/v1/admin/data/retention/status` (any authenticated role): last run + per-class backlog. **VERIFIED**.
10. **Config validators** -- production fails closed when any retention setting is unset, when the pepper is
    missing/weak/equal to another secret, or processed-event retention is below the floor. **VERIFIED**.

## 5. Settings

| Setting | Default | Production |
|---|---|---|
| `RETENTION_TRANSCRIPT_DAYS` | unset = keep forever | must be set (0 = forever) |
| `RETENTION_ANALYSIS_DAYS` | unset | must be set |
| `RETENTION_CONTACT_DAYS` | unset | must be set |
| `RETENTION_AUDIT_LOG_DAYS` | unset | must be set |
| `RETENTION_PROCESSED_EVENT_DAYS` | unset | must be set; `>= 7` or 0 |
| `RETENTION_RUN_HISTORY_DAYS` | unset | must be set |
| `RETENTION_BATCH_SIZE` | 500 (1..10000) | |
| `RETENTION_MAX_RUNTIME_SECONDS` | 300 | |
| `ERASURE_FINGERPRINT_PEPPER` | empty (dev derives one from the JWT key) | required, >= 32 chars, distinct |

No recording, storage, lease or presign settings exist yet (CP15C).

## 6. PRODUCT / LEGAL DECISIONS awaiting owner confirmation

The branch's decision table (decision 4) says ship retention OFF; this PR honours that: **no value is baked in**, nothing
is deleted until the owner sets the periods. The proposed values below are only in `.env.example` comments.

| # | Decision | Value chosen / proposed |
|---|---|---|
| A1 | Retention periods (days) | transcripts 180, analysis free-text 365, contact PII 365 after campaign close, audit 730, processed events 90, run history 365 -- **owner to set** |
| A2 | Processed-event floor | 7 days, an **assumption** (real Dograh retry horizon UNVERIFIED) |
| A3 | "Campaign closed" | `campaign.closed_at`, set when status becomes COMPLETED; existing COMPLETED campaigns are backfilled only from their `campaign.status_changed` audit row, otherwise NULL = never auto-purged |
| A4 | Erasure suppresses by default | yes (`suppress=true`), opt-out allowed; the suppression row necessarily keeps the number (legal basis for DNC is the owner's call) |
| A5 | Transcript retention keeps | attempt state, duration, scores, enums; deletes turn text and clears working-memory content |
| A6 | Held data also protects | audit rows whose `entity_id` is the held contact/campaign |
| A7 | Roles | the repo has `admin` and `operator` only; there is no `viewer` role. Erasure/export/hold = admin; status = any authenticated role |

## 7. Known limitations / DOCUMENTED LIMITATIONS

- Dograh-side audio/transcripts cannot be deleted from this repo (no documented API).
- Redis working-memory cache (`MemoryStore`, TTL 3600 s) can hold captured entities/last utterance; erasure and retention do not delete cache keys (expire within 1 h). Other Redis payloads were not exhaustively audited: UNVERIFIED.
- Backups pre-dating an erasure still contain the person until they age out; `reapply_erasures` re-scrubs after a restore. The pepper must be backed up with the secrets, or fingerprints from before a loss cannot be matched.
- `reapply_erasures` cannot recreate a lost DNC row (the number is not stored); it reports the gap for CP15B's secondary DNC copy.
- A transcript is not deleted while its analysis is PENDING/PROCESSING/RETRY_WAIT (so a stuck analysis blocks its transcript purge).
- Retention status backlog uses live COUNTs: fine now, may need bounding at large scale (CP17).
- Sandbox is Python 3.12; the production image is `python:3.11-slim`. Code uses only 3.11-compatible features (`StrEnum`, `X | None`), ruff targets py311, but a 3.11 run was **not** executed: UNVERIFIED.
- Postgres tests ran on 16 only.

## 8. Deferred / blocked

- CP15B (backup, PITR, restore drills, DNC secondary copy, runbook); CP15C (recording) -- BLOCKED on decision 9; S3 backend BLOCKED (boto3 not approved).
- Moving the transcript fetch out of the webhook transaction: DEFERRED (changes analysis trigger timing, owned by CP14B).
- No scheduler is added: schedule `run_retention` with cron/CronJob (see deploy step 5).
- Legal review of consent/disclosure: owner-owned, DEFERRED.

## 9. Deploy steps

1. Back up, then `alembic upgrade head` (adds columns/tables/indexes; safe on populated data).
2. Set **every** `RETENTION_*_DAYS` explicitly (0 = forever) and `ERASURE_FINGERPRINT_PEPPER` (and store the pepper in the secret manager) -- production will not start otherwise.
3. Run `python -m app.scripts.run_retention` (dry run) and review the counts; check `GET /api/v1/admin/data/retention/status`.
4. Place legal holds on anything that must be preserved (`PUT .../legal-hold/...`) BEFORE the first `--apply`.
5. Schedule `python -m app.scripts.run_retention --apply` daily off-peak (e.g. `17 3 * * *`, or a Kubernetes CronJob with `concurrencyPolicy: Forbid`). Exit codes: 0 done, 2 partial (run again), 3 lock held, 1 failed.
6. After any restore: `python -m app.scripts.reapply_erasures` (dry run), then `--apply`.

## 10. Rollback

- Code: revert the PR. Retention changes are not reversible (data deleted/scrubbed stays so); that is why step 3-4 precede `--apply`.
- Schema: `alembic downgrade a83d13f0c415` refuses while `erasure_log` has rows or any legal hold is set; the guard message explains the deliberate steps (the log is append-only; deleting it needs an explicit `ALTER TABLE ... DISABLE TRIGGER`). Forward-fix is preferred.

## 11. Changes to existing tests (and why)

- `tests/test_cp11_production_config.py`, `tests/test_cp10_config.py`: the production fixtures now spread `CP15_PRODUCTION_SETTINGS` -- required because production refuses to boot without the new explicit settings. No assertion changed.
- `tests/test_cp14_migrations.py::test_a_single_head_...`: pinned head moved to `c15a1b2c3d4e` (same pattern CP14B used); the single-head and parent-chain assertions are kept and extended.

## 12. Final gates

`ruff check .` clean; `mypy app` clean (173 files); `alembic upgrade head` + `alembic check` clean, single head `c15a1b2c3d4e`;
`pytest`: **1587 passed**, run twice for repeatability (1506 baseline on this branch + 81 new).

## 13. Acceptance checklist

- [x] Registry covers every column; guard fails on drift
- [x] Retention dry-run default, advisory lock, counts-only audit
- [x] Suppression never auto-purged (guard + behavioural test)
- [x] Legal hold blocks retention and erasure
- [x] Erasure across campaigns, fingerprint log, suppression by default, idempotent
- [x] `reapply_erasures` after a simulated restore
- [x] Production refuses unset retention / weak pepper
- [x] Migration reversible on populated data
- [ ] Owner confirms A1-A7
- [ ] Python 3.11 run (UNVERIFIED)
