# HF1 — Safety follow-ups (hardening follow-up 1 of 3)

Branch `feature/hf1-safety-followups`, from `develop` at `51def6a` (CP15B merged). The only HF PR
with a migration (`e2d1c0b9a8f7`, one, reversible).

Labels used on every statement: **VERIFIED** (proven by a test or an executed command),
**UNVERIFIED**, **BLOCKED**, **DOCUMENTED LIMITATION**, **DEFERRED**, **OWNER DECISION**.

## 0. Current state (read this first)

| Item | State |
|---|---|
| 1.1 authenticate first, then limit | DONE, VERIFIED |
| 1.2 trusted-proxy client address | DONE, VERIFIED |
| 1.3 durable kill switch | DONE, VERIFIED |
| 1.4 production exposure | DONE, VERIFIED |
| 1.5 provider configuration errors | DONE, VERIFIED (against a deterministic Dograh fake) |
| 1.6 `call_analysis.conversation_session_id` index | DONE, VERIFIED |
| Branch pushed / PR opened | DONE: PR #55 against `develop` (not merged; the owner reviews and merges) |
| HF2, HF3 | NOT STARTED (one PR at a time, as instructed) |

## 1. Objective and scope

Close the six audit findings against `develop` (rate limiter ordering, proxy handling, Redis-only
kill switch, API schema exposure, burned contacts on provider configuration errors, an unindexed
FK) without changing anything outside that scope. Each defect has a failing reproduction commit
before its fix commit (see §11).

## 2. Environment and baseline (before any change)

* Python **3.12.3** ran everything below; CI and the image use 3.11. **DOCUMENTED LIMITATION**:
  nothing here was run on 3.11.
* PostgreSQL 16 + Redis 7 (`--dir /tmp`, no `dump.rdb` in the repo), `age` and `shellcheck` installed.
* Baseline: `ruff` clean, `mypy app` clean (177 files), one alembic head `c15a1b2c3d4e`,
  `alembic check` clean, `pytest` **1697 passed, 0 skipped**. **VERIFIED**
* Frontend baseline: `npm ci` **fails** (`ERESOLVE`: `@types/node@^20` vs `vitest@5` needing
  `^22 || >=24`); with `--legacy-peer-deps` (what CI does) `tsc`, lint, 23 tests and `build` pass.
  **VERIFIED**; this is an HF2 item and was not touched.

## 3. Fixes

### 3.1 Authenticate first, then limit — VERIFIED
`tests/test_hf1_auth_then_limit.py`. Reproduced on unmodified code: after 121 bad requests a request
with the CORRECT secret was answered 429 (both webhook routes); ten failed logins for another user
locked out the real admin; the fourth *successful* login was throttled; the threaded test caught a
Redis key with no TTL (the INCR/EXPIRE gap; probabilistic by nature).

* Dograh and native telephony webhooks verify the secret first (constant-time, bytes, as before).
  A valid secret is subject only to `WEBHOOK_VALID_CEILING_PER_MINUTE` (default 6000, above admission
  CPS x 60 = 1200). A failed authentication increments a per-client-address counter and is answered
  401, and 429 only once that counter is over `WEBHOOK_RATE_LIMIT_PER_MINUTE`.
* Login counts **failed** attempts only, in two buckets: per (client address, normalised+hashed
  username) at `LOGIN_RATE_LIMIT_PER_MINUTE`, and per address across usernames at
  `LOGIN_IP_FAILURE_CEILING_PER_MINUTE` (default 50). A correct credential is blocked only by
  failures from its own client address.
* The limiter is one Lua script (INCR; TTL set on creation or healed if ever missing): a key can
  never exist without a TTL. VERIFIED (8 threads x 40 calls on real Redis: exactly `limit` pass).
* Fallbacks unchanged: webhooks fail open with an audit row; login fails **closed** (503), including
  for a correct credential. VERIFIED (existing tests, still green).
* The webhook auth-failure audit events and the login events use the shared client-address helper.
* **DOCUMENTED LIMITATION** (residual denial of service): anyone sharing a client address (a NAT or
  office egress) can slow logins for that address's username; a distributed attacker cannot lock
  a username out for *other* addresses, but still gets its own guesses slowed, not stopped.
* Login success does not reset the failure counters (window expiry only).

### 3.2 Client address behind a proxy — VERIFIED
`app/core/client_address.py`, `tests/test_hf1_client_address.py` (37 tests).
`TRUSTED_PROXIES` (CIDRs, default empty = socket peer, header ignored). `X-Forwarded-For` is honored
only when the socket peer is inside a trusted CIDR, and then the **right-most entry not inside a
trusted CIDR** is the client; a malformed right-most entry falls back to the peer (it never skips to
a client-controlled left value); multiple header lines are one list; IPv4-mapped IPv6 is normalised;
ports/brackets tolerated; chain length and header size bounded; nothing raises. A malformed CIDR is
a **startup error** (Settings validation). The Dockerfile CMD is untouched and a test asserts it
contains neither `--proxy-headers` nor `--forwarded-allow-ips`.

* IPv6 clients are bucketed by /64 for rate limiting (one host owns 2^64 addresses). **OWNER DECISION**
  (my choice; revert by changing `rate_limit_identity`).
* **UNVERIFIED**: behavior behind a real reverse proxy; only constructed requests were tested.

### 3.3 Durable kill switch — VERIFIED
`app/services/outbound_control.py`, `app/models/outbound_control.py`, `tests/test_hf1_kill_switch_durable.py`.
Reproduced on unmodified code: after a Redis flush the switch read `None` (dialing allowed).
PostgreSQL is the source of truth (`outbound_control`, row `kill_switch`; `provider_halt` shares the
table). Dialing is blocked when the row is engaged, **or** `OUTBOUND_KILL_SWITCH=true`, **or** the
state cannot be read (fail closed, `kill_switch_unreadable_failing_closed` error log with no
exception text). Redis is not consulted at all (a test makes any Redis use raise).

* Engage/release are single atomic statements: 16 concurrent engages produce exactly one transition.
* In-process cache `KILL_SWITCH_CACHE_SECONDS` (default 2; injectable clock); a local change is
  visible at once, a change by another process within the window; a failed read is never cached.
* API shape unchanged (`POST`/`DELETE /api/v1/admin/kill-switch`, admin-only release, audit events as
  before). New: `released_by` is stored.
* Queued jobs and scheduled retries are untouched while engaged (nothing acked, nothing discarded,
  no attempt claimed), including after a Redis flush. VERIFIED
* **OWNER DECISION**: the engage `reason` stays **optional** (the brief says "mandatory", but the
  existing test `test_enable_with_no_body_is_accepted` pins no-body engaging and I may not weaken it).
  Say if you want it mandatory; it is a one-line change plus that test.
* **DOCUMENTED LIMITATION**: a switch flipped by another process is honored by a worker within
  `KILL_SWITCH_CACHE_SECONDS`; the pre-existing race (flip after the final gate read but before the
  HTTP trigger) is unchanged.
* **DOCUMENTED LIMITATION**: the PostgreSQL read happens on the worker's own short session; a long
  database outage blocks all dialing (by design: fail closed).

### 3.4 Production exposure — VERIFIED
`app/main.py` now has `create_app(settings)`. In `staging`/`production`: `/docs`, `/redoc`,
`/openapi.json` (and the oauth redirect) return 404; development/test keep them. `/health` returns
`{"status", "service"}` with no `environment`; `/ready` never carried it. `/ready` returns no exception
text, host names or secrets: tested with a leaky component detail and with a genuinely unreachable
Redis whose URL contains a password and host.

* **OWNER DECISION**: `/health` keeps `service` (the brief says "only status" but names only
  `environment` for removal, and `test_health` pins `service`).

### 3.5 Provider configuration errors must not burn contacts — VERIFIED (fake provider)
`app/services/provider_halt.py`, dialer worker, `tests/test_hf1_provider_halt.py` (32 tests).
Reproduced on unmodified code with the audit's scenario: 30 queued contacts + HTTP 401 ->
`assert 5 == 1` (5 provider calls). Now: **exactly one provider call, zero terminal contacts, all 30
dialable, `provider_halt` engaged, one audit event**. Recovery after release with a working key dials
everyone **exactly once** on one attempt row each, whether the held job is re-driven from the stream or
the contacts are rebuilt from PostgreSQL after a full Redis wipe (`POST /campaigns/{id}/enqueue`).

**Current mapping of provider error class to outcome** (source: `docs/CHECKPOINT-13-NOTES.md` §4, code
`dograh_outcome.py`), and what HF1 does:

| Status | CP13 policy (unchanged in the table) | Before HF1 | After HF1 |
|---|---|---|---|
| 2xx + run id | accepted | dialing, streak reset | same (+ resets the systemic streak) |
| 2xx malformed | ambiguous -> reconcile | same | same |
| 400 | permanent (mixed: telephony unconfigured **or** initiation failed) | contact terminal | **per-contact**, unchanged |
| 401 / 403 | permanent | contact terminal | **configuration**: held + halt on first error |
| 402 | permanent (quota exhausted) | contact terminal | **configuration** (`quota_exhausted`) |
| 404 | permanent (trigger missing/inactive) | contact terminal | **configuration** (`provider_rejected`) |
| 408, 500, 502, 504, read timeout | ambiguous -> reconcile | unchanged | unchanged |
| 409, 422 | permanent | contact terminal | **per-contact**, unchanged |
| 429 | rate limited, bounded retry | unchanged | unchanged |
| 501, 503, connect failure | transient, budgeted | unchanged | unchanged |
| client cannot be built (missing key/trigger) | recorded, handed to RecoveryManager | contact terminal | **configuration** (`configuration_error`) |

`classify_trigger_error` and the CP13 policy table are untouched (the old table test still passes);
the new `provider_configuration_category(exc)` is an additive question the dialer asks.

On a configuration error the worker: records the attempt (`FAILED_TO_CONNECT`, no provider call) and a
`DOGRAH_PROVIDER_HALT_HELD` event whose payload is only a sanitized category and status code; restores
the contact to its exact prior status and attempt count (a first attempt is `PENDING`/0; a held
**retry** is restored to its scheduled state — tested); leaves the job **unacked** (`PROVIDER_HALTED`);
and engages `provider_halt` on the **first** error (`engaged_by` = `system`; reason from a fixed
vocabulary; one audit event written in the same transaction as the transition). After release the
held attempt row is **reused**, so the contact is dialed once. The circuit breaker is not fed by these
errors (the halt replaces it).

* Systemic detector: `PROVIDER_HALT_SYSTEMIC_THRESHOLD` (default 5) consecutive permanent per-contact
  failures with the same category across **different** contacts (Lua set in Redis; any accepted trigger
  or a different category resets it) engage `provider_halt` with category `systemic_failure`, and the
  contact that trips it is held, not burned. VERIFIED
* **DOCUMENTED LIMITATION**: the first N-1 contacts of a systemic streak had already been made
  terminal (today's per-contact behavior) before the Nth trips it; reverting terminal contacts was out
  of scope and risky. They are `CompletedPartial` with audit events and can be reset by the owner.
* Visibility: `GET /api/v1/admin/kill-switch` now includes `provider_halt` (engaged, reason, category,
  who, when, released by/at); `GET /api/v1/admin/provider-halt` too. Release is
  `POST /api/v1/admin/provider-halt/release` (admin only, mandatory non-blank reason, audited, idempotent).
  VERIFIED
* No credential or raw provider body reaches the DB, audit rows, call events, logs or API responses:
  tested with a secret embedded in the fake provider's error text. VERIFIED
* **OWNER DECISION**: 400, 409 and 422 stay per-contact (400 is mixed free text; 409/422 share a CP13
  row with "bad request"). Their safety net is the systemic detector. 409 ("no execution owner") is
  arguably a workflow configuration problem; say if you want it classified as configuration.
* **OWNER DECISION**: enqueue (`POST .../enqueue`) is **not** blocked by `provider_halt` (only by the
  kill switch, as before): jobs queue and wait unacked. Say if enqueue should refuse while halted.
* **DOCUMENTED LIMITATION**: a held attempt counts toward the daily spend/attempt budget by its
  `started_at` (bounded by the number of in-flight workers; reuse resets `started_at`, so it is counted once).
* **DOCUMENTED LIMITATION**: if the halt itself cannot be written (database down at that instant) the
  exception propagates and the job stays pending; the claim committed just before the trigger is then
  resolved by the existing orphaned-claim path (ambiguous recovery), as for any crash after the claim.
* **UNVERIFIED**: the real Dograh response shapes for these statuses (only CP13's documented table and a fake).

### 3.6 Schema hygiene — VERIFIED
Index `ix_call_analysis_conversation_session_id`. The migration adds nothing else besides
`outbound_control`. Upgrade, downgrade (to `c15a1b2c3d4e`) and re-upgrade are clean on a scratch
database that already holds campaign/contact/attempt/session/analysis rows
(`tests/test_hf1_migration.py`, 5 tests), and `alembic check` reports no drift. The downgrade
**refuses** while any control is engaged (dropping the table would silently lift a stop).
`outbound_control` is classified in the CP15A data inventory (required by its guard test;
operator reason/usernames are classed `PII_INDIRECT`, like `audit_log`) and `docs/DATA-INVENTORY.md`
was regenerated (8 added rows, nothing else).

## 4. Settings added

| Setting | Default | Meaning |
|---|---|---|
| `TRUSTED_PROXIES` | empty | CIDRs whose `X-Forwarded-For` is honored; malformed = startup error |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` (existing) | 120 | now: FAILED webhook authentications per client address |
| `WEBHOOK_VALID_CEILING_PER_MINUTE` | 6000 | ceiling for requests with a valid secret |
| `LOGIN_RATE_LIMIT_PER_MINUTE` (existing) | 10 | now: FAILED logins per (address, username) |
| `LOGIN_IP_FAILURE_CEILING_PER_MINUTE` | 50 | FAILED logins per address across usernames |
| `KILL_SWITCH_CACHE_SECONDS` | 2 | in-process cache of the PostgreSQL stop flags (0 = off) |
| `PROVIDER_HALT_SYSTEMIC_THRESHOLD` | 5 | consecutive same-category permanent failures that halt |
| `PROVIDER_HALT_STREAK_TTL_SECONDS` | 3600 | how long a streak is remembered |

All are in `backend/.env.example` (the root `.env.example` documents none of these areas; completing
the three example files is HF3).

## 5. Deploy steps

1. Deploy the image with the migration: `alembic upgrade head` (adds `outbound_control` and the index;
   no backfill). VERIFIED on a scratch DB with rows.
2. **A kill switch engaged only in Redis before this deploy is NOT carried over.** Immediately after
   deploying, if it should still be on, re-engage it: `POST /api/v1/admin/kill-switch` with a reason.
   (Or set `OUTBOUND_KILL_SWITCH=true` for the deploy window.) No Redis-to-DB importer exists by design.
3. Set `TRUSTED_PROXIES` to your reverse proxy's CIDR(s) if the API sits behind one; otherwise every
   client shares the proxy's address and the per-address limiters become one bucket.
4. In staging/production `/docs`, `/redoc` and `/openapi.json` now 404, and `/health` has no
   `environment`: update any monitor or script that read them. (The frontend does not.)
5. Operators: an engaged `provider_halt` is visible at `GET /api/v1/admin/kill-switch`. After fixing the
   key/trigger/quota, release it with `POST /api/v1/admin/provider-halt/release {"reason": "..."}`.
   Held jobs resume on the next reclaim; contacts need no manual action.

## 6. Rollback

* Code: revert the PR. While a control is engaged the migration downgrade **refuses**: release it first.
* `alembic downgrade c15a1b2c3d4e` drops `outbound_control` and the index; the previous code returns to
  the Redis-only kill switch (state is then lost; re-engage via the old API if needed).
* A contact held by the halt is an ordinary `PENDING`/retry contact, so rolling the code back loses nothing,
  but the held attempt row (`FAILED_TO_CONNECT`, no provider call) is, to the OLD code, an already
  resolved attempt: a re-driven or re-enqueued job for it is answered `ALREADY_PROCESSED` and acked
  (verified by reading `_resolve_unresolved_dograh_claim` on `develop`), so that contact would stay
  `PENDING` and never be dialed. **DOCUMENTED LIMITATION**: release the halt and let the new code drain
  held jobs before rolling back, or delete those held attempt rows by hand.

## 7. Existing tests changed, and why

No assertion was deleted. Where a test pinned behavior the brief reverses, only that assertion was
rewritten, branch-wise, with an `HF1` comment; everything that must still hold keeps its exact assertion.

| Test | Change | Why |
|---|---|---|
| `test_cp11_rate_limit::test_a_rejected_caller_is_rejected_even_with_a_valid_secret`, `test_rate_limiting::test_webhook_is_rate_limited_after_threshold` | same assertion, driven by `webhook_valid_ceiling_per_minute` | valid secrets no longer share the failed-auth limit (that was the defect) |
| two fake-Redis classes in limiter tests | gained `eval()` raising `RedisError` | the limiter is one Lua script now |
| `test_cp11_kill_switch` | fixture `dead_redis` -> `unreadable_store` (PostgreSQL unreadable) | the store is PostgreSQL; same fail-closed assertions |
| `test_health` | `environment` asserted **absent** | brief 1.4 |
| `test_cp14_migrations` | head pin -> `e2d1c0b9a8f7`, chain link added | per the file's own precedent for each checkpoint |
| `test_cp10_dialer_failures` (401/404 params, config-error test), `test_cp13_dograh_critical::test_permanent_provider_faults_are_never_retried` (401/402/403/404/config params) | configuration cases assert held/halted instead of terminal/acked | brief 1.5; the 400/409/422 cases are unchanged |
| `tests/conftest.py` | `KILL_SWITCH_CACHE_SECONDS=0` default; per-test clearing of `outbound_control` | the flags are committed to PostgreSQL, so the per-test rollback does not undo them |

## 8. Found, not fixed (adjacent, out of scope)

* The generic `rate_limit_dependency` (enqueue, import, mutations, analysis) still counts every request
  per client address; only the three pre-authentication endpoints changed.
* `request.client.host` is still used nowhere else, but no test enforces that new code uses the helper.
* `/health` still exposes `service` (see §3.4).
* The first N-1 contacts of a systemic streak stay terminal (see §3.5).
* Login success does not clear failure counters.
* `provider_halt` reasons are English strings in the database (no i18n).

## 9. UNVERIFIED / DEFERRED

* **UNVERIFIED** on Python 3.11 (ran 3.12.3). **UNVERIFIED** against a real reverse proxy and against real
  Dograh. **UNVERIFIED**: the GitHub Actions run (HF1 changes no workflow).
* **DEFERRED** to HF3: completing `.env.example` coverage, logging configuration, a test that every
  Settings field is in the example file.

## 10. Acceptance checklist

- [x] Preconditions verified: CP15B (PR #53) is in `develop`; baseline recorded before any change
- [x] Every defect has a failing reproduction committed before its fix
- [x] `ruff check .`: clean; `mypy app`: no issues (182 files)
- [x] `alembic heads`: one (`e2d1c0b9a8f7`); `alembic upgrade head` + `alembic check`: no drift;
      `downgrade -1` then `upgrade head` round-trip: clean
- [x] Full `pytest`: **1814 passed, 0 skipped** (1697 baseline + 117 new) on Python 3.12.3
- [x] No secrets, tokens or real numbers in code, tests, docs, logs or commits (fixtures use RFC 5737
      addresses and an obviously fake `dg_live_secret_abc123`)
- [x] No new runtime dependency; one migration; nothing merged or pushed to `develop`/`main`
- [x] PR #55 opened against `develop`, not merged or approved — the owner reviews and merges

## 11. Commits (failing test first, then the fix)

```
de91ed9 HF1 1.1: failing reproductions for authenticate-then-limit
cf15568 HF1 1.2: failing tests for the shared trusted-proxy client address helper
4d1905e HF1 1.2: shared trusted-proxy client address helper, TRUSTED_PROXIES setting
835a106 HF1 1.1: authenticate first, then limit; atomic Lua limiter
7903ead HF1 1.1: fix mypy and ruff line lengths
f6374aa HF1 1.4: failing tests for production exposure
770b486 HF1 1.4: create_app factory; docs disabled in staging/production; /health without environment
280aa8f HF1 1.3/1.6: failing tests for the durable kill switch and the migration
25c8508 HF1 1.3/1.6: durable kill switch in PostgreSQL; migration e2d1c0b9a8f7
ddb8985 HF1 1.5: failing tests for provider configuration errors
5026678 HF1 1.5: provider configuration errors hold + halt; systemic detector; admin release
```

## 12. File overlap with HF2 / HF3 (for the PR description)

`backend/app/core/config.py` and `backend/.env.example` (HF3 adds logging settings and completes the
example), `backend/app/main.py` (HF3 logging setup), `backend/tests/conftest.py`. HF2 should not overlap
beyond `requirements*.txt`/workflows, which HF1 does not touch.
