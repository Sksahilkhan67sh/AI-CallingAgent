# Checkpoint 11 — API Security & Tenant Isolation (notes)

Status: implemented and tested in a sandbox. **Not production verified.** Live Dograh
verification is **BLOCKED** (no credentials were available). Schema migration: **none required**.

Base: `origin/develop` (CP10 is merged into `develop`, not into `main`).

---

## 0. What this checkpoint is, and is not

CP11 hardens the API and the outbound path. It deliberately does **not** introduce
multi-tenancy. The data model has no `tenant_id` / `owner_id` / user table, and the CP11
decision was *Option A: stay single-tenant, document tenant isolation as DEFERRED*.
Everything below must be read with that in mind (see §6 and §15).

## 1. Security architecture

```
request
  → RequestIdMiddleware        (correlation id, outermost)
  → CORSMiddleware
  → BodySizeLimitMiddleware    (413 before any parsing or auth)
  → route dependencies, in this order:
        1. identity   → verified bearer token (401)            [authentication]
        2. budget     → per-principal rate limit (429 / 503)   [abuse control]
        3. role       → require_role("admin") (403 + audit)    [role authorization]
  → request validation (422)
  → service  → repository
  → (enqueue only) global kill switch → existing admission rules
```

Dial path (worker): `read → [kill switch stage 0] → process_claimed_job → [stage 1] →
AdmissionController → _dial → eligibility → [stage 2] → durable attempt claim → Dograh trigger`.

Authorization is expressed as named dependencies on the route decorator or signature
(`_ADMIN`, `_ANY_ROLE`, and the budgets in `app/api/route_limits.py`), not as ad-hoc checks inside
handlers. A structural test fails if any `/api/v1` route (other than login and the two webhooks)
lacks the bearer dependency.

## 2. Authentication model

* Existing mechanism, reused and hardened: HS256 JWT issued by `POST /api/v1/admin/auth/login`
  (`sub`, `role`, `iat`, `exp`; lifetime `JWT_EXPIRY_SECONDS`, default 3600 s).
* Two configured identities (`ADMIN_USERNAME/PASSWORD`, `OPERATOR_USERNAME/PASSWORD`). There is
  **no user table**.
* Missing / malformed / expired / wrongly-signed / unknown-role / empty-claim / `alg:none` tokens → **401**
  with `WWW-Authenticate: Bearer`. Identity and role come only from the verified token; request bodies,
  query strings and headers (`role`, `user_id`, `tenant_id`, `X-User`, …) are never read for identity.
* Credential comparison is constant-time on UTF-8 bytes (non-ASCII input no longer causes HTTP 500).
* **Not implemented:** token revocation, logout, refresh tokens, MFA, per-user accounts.

## 3. Authorization model

| Layer | Question | Mechanism |
|---|---|---|
| Authentication | who is calling | `require_admin` (valid token) |
| Role | may this role do this | `require_role("admin")` |
| Resource | does this principal control this object | **not applicable** (single tenant, §5–6) |
| System | is outbound currently allowed | global kill switch (§11) |

## 4. Roles

| Role | Permitted |
|---|---|
| `admin` | all reads; campaign/contact create, update, associate, remove, deactivate; CSV import; campaign status change; enqueue; kill switch enable/disable |
| `operator` | **read-only**: lists/details of campaigns, contacts, call attempts, dashboards, analysis, kill-switch *status* |

No operator write was justified by an existing business requirement, so none was granted.

## 5. Resource ownership rules

There are none beyond role. Any authenticated principal can read **every** campaign, contact and
analysis (including transcripts). This is the pre-existing admin-dashboard behaviour for operators and
is a consequence of the single-tenant model — see Known limitations.

Because ownership does not exist, the "cross-resource IDOR" tests in the CP11 brief
(USER A → campaign B denied) **cannot be written truthfully**. What *is* tested: identity/role are
taken only from the token, forged `tenant_id` / `owner_id` / `created_by` / `role` / `actor` fields are ignored
and not persisted, and operators are denied every mutation.

## 6. Tenant isolation status

**Full multi-tenant schema isolation is NOT present. DEFERRED** until a real tenant/owner model is
introduced (see Deferred items for what that requires). No `tenant_id` / `owner_id` column, no user
table and no speculative migration were added.

## 7. API protection matrix

Generated from the live route table (36 routes). "Limit" budgets are per principal per minute unless noted.

| Endpoint | Auth | Role | Resource check | Rate limit |
|---|---|---|---|---|
| `POST /api/v1/admin/auth/login` | public | – | – | IP, 10/min, fail **closed** (503) |
| `GET  /api/v1/admin/{call-attempts,campaigns,contacts}` (+ `/{id}`) | bearer | any | none (single tenant) | none |
| `GET  /api/v1/admin/dashboard/{overview,analytics,system}` | bearer | any | none | none |
| `POST /api/v1/admin/campaigns/{id}/status` | bearer | admin | none | mutation 120 |
| `GET  /api/v1/admin/kill-switch` | bearer | any | – | none |
| `POST/DELETE /api/v1/admin/kill-switch` | bearer | admin | – | mutation 120 |
| `GET  /api/v1/campaigns`, `/{id}`, `/{id}/contacts`, `/{id}/contacts/counts` | bearer | any | none | none |
| `POST /api/v1/campaigns`, `PATCH /{id}` | bearer | admin | none | mutation 120 |
| `POST /api/v1/campaigns/import` | bearer | admin | none | **import 5**, fail closed; body ≤ 6 MiB, file ≤ 5 MiB |
| `POST/DELETE /api/v1/campaigns/{id}/contacts/{cid}` | bearer | admin | none | mutation 120 |
| `POST /api/v1/campaigns/{id}/enqueue` | bearer | admin | none | **enqueue 5**, fail closed; kill switch |
| `GET  /api/v1/contacts`, `/{id}` | bearer | any | none | none |
| `POST /api/v1/contacts`, `PATCH /{id}`, `POST /{id}/deactivate` | bearer | admin | none | mutation 120 |
| `GET  /api/v1/{campaigns,contacts}/{id}/analysis`, `GET /api/v1/call-attempts/{id}/analysis` | bearer | any | none | analysis 120 |
| `POST /api/v1/webhooks/dograh/call-completed` | shared secret | – | attempt/run ownership (CP08–10) | IP, 120/min, fail **open** + audit |
| `POST /api/v1/webhooks/telephony/call-status` | shared secret | – | provider call id | IP, 120/min, fail **open** + audit |
| `GET /health`, `GET /ready` | public | – | – | none |

Read endpoints other than analysis are **not** rate limited (authenticated only).

## 8. Rate limiting

* One limiter (`app/core/rate_limit.py`), Redis fixed window (INCR + EXPIRE). No second framework.
* Verified **enforced**, not merely configured (tests drive real 429s). Settings are now read per request
  (`develop` read the webhook limit once at import time).
* Identity: client IP before authentication (login, webhooks); the **verified token subject** for
  authenticated budgets. Rotating `X-Forwarded-For`/`X-Real-IP`, trailing-slash variants, or a second
  token for the same user does not reset a budget. `X-Forwarded-For` is deliberately not parsed.
* `429` carries `Retry-After` (seconds left in the window). A key that somehow lacks a TTL
  (crash between INCR and EXPIRE) is repaired when blocked instead of blocking forever.
* Memory is bounded (keys expire after one window).

Redis-failure policy (user-decided):

| Control | Redis unavailable |
|---|---|
| Kill switch read, final outbound admission, enqueue | **fail closed** |
| Login limiter | **fail closed** (503, even for correct credentials) |
| Enqueue / import budgets | **fail closed** (503) |
| Dograh + telephony webhook limiter | **fail open**, one audit row per minute per process + a log per request |
| Ordinary mutation / analysis budgets | fail open + audit (my choice, see Deferred) |
| **Webhook authentication** | **never fails open** (401); idempotency is DB-backed and independent of Redis |

## 9. Webhook security

Order: rate limit → authentication → body validation → idempotency → ownership → state transition → DB.
Both webhooks now authenticate **before** the body is validated (the legacy telephony route previously leaked
schema feedback via 422 and compared with `!=`).

* Dograh authentication is a **shared secret** (`Authorization: Bearer` or `X-API-Key`), constant-time on bytes.
* **There is no HMAC and no timestamp/replay window.** CP10 documents that Dograh does not provide signing and none
  was invented. Replay protection is the DB-backed `ProcessedEvent` idempotency only; TLS and secret secrecy are the
  real controls.
* Preserved: workflow_run_id validation, attempt ownership, mismatch protection, duplicate = no-op.
* New audit/log trail: `webhook.auth_failed` (never the credential; only whether one was presented) and `webhook.duplicate`.
* Kill switch does **not** block completion webhooks: a call already in progress must still be recorded.

## 10. SSRF protection (`app/services/telephony/transcript_fetch.py`)

* Schemes: `https` only in production; `http`/`https` otherwise. No URL credentials.
* Untrusted hosts: default port only. Trusted hosts (Dograh's own host, `DOGRAH_TRANSCRIPT_EXTRA_HOSTS`): any port.
* Every resolved address is checked. Loopback, link-local (incl. cloud metadata), unspecified, multicast,
  reserved are blocked **for everyone, trusted hosts included**. Other non-public ranges (RFC1918, ULA, CGNAT
  `100.64/10`, documentation ranges) are allowed only for trusted hosts. IPv4-mapped IPv6 is judged by the embedded IPv4
  (interpreter classification of `::ffff:x` differs between Python patch releases, so it is done explicitly).
* **DNS rebinding is closed, not just documented:** resolve once, validate all addresses, connect to the validated
  IP with the original `Host`/SNI, no second lookup. `trust_env=False` so a proxy variable cannot reroute it.
* Redirects are **never** followed (a 3xx is a failed fetch; nothing is requested at the target).
* Connect 5 s / read 10 s / total 20 s deadline; decoded body ≤ `DOGRAH_TRANSCRIPT_MAX_BYTES` (2 MB), streamed;
  ≤ `DOGRAH_TRANSCRIPT_MAX_LINES` (5,000) stored; content-type allow-list (`application/json`, `+json`,
  `text/json`, `text/plain`, `application/octet-stream`).
* Logs carry reason codes only — never the URL (it may be a signed URL).
* Residual: a **trusted** host is trusted with private addresses by design; a compromised Dograh can serve content up to the caps.
  Other IPv6 transition ranges (6to4, Teredo) are not specially handled — **UNVERIFIED**.

## 11. Global kill switch

**Purpose:** stop NEW outbound calls immediately. It never deletes queued jobs and never terminates an active call.

* Enable: `POST /api/v1/admin/kill-switch {"reason": "..."}` (admin). Disable: `DELETE` (admin). Status: `GET` (any role).
* Storage: Redis key `outbound:kill_switch` (`SET NX` / `DEL`, so concurrent toggles yield exactly one transition and one
  audit row), plus a static backstop `OUTBOUND_KILL_SWITCH=true` (needs a restart; checked first; cannot be cleared by the API).
* **Propagation:** no cache — the next read sees it. Idle workers notice within one poll (`block_ms` = 1 s).
* **Redis failure → blocked.** An unreadable switch is treated as ON everywhere (enqueue → 503; worker → no dial).
* Stages:
  0. *Pre-read* (`process_one_job`): a blocked worker does not read from the stream. Backlog stays in the stream and
     resumes at full speed. (Without this, a frozen backlog would migrate to the pending list, which is re-driven only
     10 jobs per ~50 loop iterations.)
  1. *Before admission* (`process_claimed_job`): job already read → left unacked; consumes no CPS/concurrency slot.
  2. *Final check* (`_dial`, after eligibility, **before** the durable attempt claim): leaves no half-claimed attempt.
* Covers every path that can place a call: first dial, queued backlog, retries and recovery re-enqueues all arrive as
  `DialJob`s. A source-level test fails if anything outside `dialer_worker.py` calls the provider entry points.
* **Residual race (not atomic):** a flip after the stage-2 read but before the HTTP trigger lets that one in-flight call
  through. The window is one DB commit plus one HTTP request. The concurrency test asserts the achievable bound: after
  `enable()` returns, plus a 0.5 s grace, no call is placed.
* Workers can each hold one already-read job at the flip; these stay unacked (≤ number of workers) and are recovered by
  `reclaim_stale` after `QUEUE_RECLAIM_IDLE_MS` (30 s).
* Recovery dispatch keeps running while the switch is on, so retries are *queued* (not dialed) and reconciliation of
  in-flight calls continues.
* Audit: `kill_switch.enabled` / `kill_switch.disabled` (actor, reason) on real transitions only; `campaign.enqueue_blocked`.

## 12. Production fail-closed rules

`staging` **and** `production` refuse to boot on any dev-only default secret. `production` additionally refuses:

* `CALLING_ENGINE` ≠ `dograh`; missing `DOGRAH_API_KEY` / `DOGRAH_TRIGGER_UUID`; `DOGRAH_TRIGGER_MODE` ≠ `production`
  (local/HTTP Dograh URL checks are pre-existing, all environments with the dograh engine).
* `JWT_SIGNING_KEY`, `TELEPHONY_WEBHOOK_SECRET`, `DOGRAH_WEBHOOK_SECRET` shorter than 32 chars or with < 8 distinct characters.
* `ADMIN_PASSWORD`, `OPERATOR_PASSWORD` shorter than 12 chars; secrets with leading/trailing whitespace.
* Any two of the five secrets (and the Dograh API key) being equal.
* Blank or identical `ADMIN_USERNAME` / `OPERATOR_USERNAME`; `*` in `ADMIN_CORS_ORIGINS`.
* Invalid enums (`ENVIRONMENT`, `CALLING_ENGINE`, `DOGRAH_TRIGGER_MODE`). All environments reject non-positive CP11 limits.

Startup errors list every problem at once, name the field only (never a value), and set `hide_input_in_errors` so
pydantic no longer appends a truncated dump of all settings (which could include `REDIS_URL` / `PRIMARY_DB_URL` credentials).

## 13. Audit logging and observability

Correlation: every request has an `X-Request-ID` (caller's if `[A-Za-z0-9._-]{1,64}`, else a fresh UUID), echoed on the
response, stamped on every log record and stored in security audit metadata.

| Event | Where | Notes |
|---|---|---|
| `auth.login_succeeded` / `auth.login_failed` | audit DB | failed: username **fingerprint** only (SHA-256/12), source IP; throttled 1/min/IP |
| `authz.denied` (403) | audit DB | role, required role, method, route template, resource ids; throttled per actor+route |
| `webhook.auth_failed` | audit DB | endpoint, credential presented yes/no, source IP; throttled 1/min/IP |
| `webhook.duplicate` | audit DB | per authenticated duplicate (bounded by the webhook limiter) |
| `kill_switch.enabled/disabled` | audit DB | real transitions only |
| `campaign.enqueue_blocked` | audit DB | kill switch |
| `rate_limit.unavailable_failed_open` | audit DB | 1/min/process |
| `auth_rejected` (401 on protected routes) | **log only** | reason code, method, route; unauthenticated callers cannot create DB rows |
| existing campaign/contact/enqueue events | audit DB | `actor` is now the verified principal, not `"api-client"` |

Rows written just before an exception are committed first (the request-scoped session rolls back on exceptions).
Every occurrence is logged even when the DB row is throttled. Never logged or stored: tokens, passwords, webhook secrets,
phone numbers, transcripts, request bodies, signed transcript URLs.

## 14. Security test coverage

`tests/test_cp11_*.py` (≈ 400 tests): endpoint authz matrix (401/403/allowed, malformed/expired/wrong-key/`alg:none`),
forged role/tenant/owner/actor fields, Unicode & malformed input, rate-limit enforcement and bypass attempts,
fail-closed/fail-open policy, body-size caps (declared, chunked, lying length), SSRF (≈ 90 cases incl. rebinding, redirects,
size, timeouts, content type), production-config rules (and boot-time behaviour in a real process), audit/durability
(against the real `get_db`), and real-thread concurrency (concurrent enqueue → no duplicate call; admin vs operator;
kill-switch toggle mid-run; pause/suppression races; forged-vs-valid webhook storms). Mutation checks were run for every
control: each was disabled in turn and the tests had to fail.

## 15. Known limitations

* **Single tenant.** Every authenticated principal sees all campaigns, contacts, transcripts and analyses. The brief's
  "operator: no unrestricted data access" is **not achievable** without an ownership model.
* No token revocation: a leaked token is valid until `exp`. Rotating `JWT_SIGNING_KEY` invalidates all tokens.
* Two shared credentials, no per-person accounts; audit actors are those two usernames.
* No webhook signing / replay window (Dograh offers none).
* Fixed-window limiter allows up to 2× the limit across a window boundary.
* Per-IP limits are only as good as `request.client.host`; behind a proxy run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>`.
* The kill switch is not atomic with the outbound HTTP trigger (§11). A Redis flush/restart without persistence clears the runtime flag
  (use AOF, or the `OUTBOUND_KILL_SWITCH` env backstop).
* Dial stream is trimmed approximately at ~200k entries (pre-existing); a very large frozen backlog could lose the oldest entries.
* Read endpoints other than analysis (e.g. dashboards) are not rate limited.
* `alembic/env.py` calls `fileConfig()` with `disable_existing_loggers=True`, silently disabling app loggers when Alembic runs in-process
  (pre-existing; affects only tests that run migrations in-process; left alone as unrelated).
* The pre-existing suite is not order independent (12 count-based tests fail when files run in reverse order, identically on untouched `develop`).

## 16. Deferred items

* Real tenant/owner model: `owner_id`/`tenant_id` on `campaign` (contacts/analyses inherit via `campaign_id`), scoped repository queries,
  per-user accounts, then the cross-tenant IDOR tests from the brief.
* Token revocation / short-lived tokens with refresh; per-user accounts.
* Rate limits on dashboard reads; optional sliding-window limiter.
* Decide whether ordinary mutation/analysis budgets should fail closed (currently fail open + audit).
* Live verification against Dograh (**BLOCKED**).
* `disable_existing_loggers=False` in `alembic/env.py`.

## 17. Operational instructions

**Deploy impact (behaviour changes):**
1. Every `/api/v1/campaigns`, `/contacts`, enqueue/import and analysis call now needs `Authorization: Bearer <token>`
   (obtain via `/api/v1/admin/auth/login`). Scripts/integrations that called these anonymously will receive 401. The dashboard already used `/admin/*` and is unaffected.
2. `staging` now refuses to boot on dev-default secrets; `production` enforces the strength rules in §12. Generate secrets with `openssl rand -base64 48`.
3. 422 responses no longer include `input`/`ctx`/`url` (shape otherwise unchanged). Login fields are capped at 256 chars.
4. JSON bodies > 1 MiB → 413 (import: 6 MiB).
5. New settings: `OUTBOUND_KILL_SWITCH`, `ENQUEUE_/IMPORT_/MUTATION_/ANALYSIS_RATE_LIMIT_PER_MINUTE`, `MAX_REQUEST_BODY_BYTES`,
   `MAX_IMPORT_BODY_BYTES`, `DOGRAH_TRANSCRIPT_MAX_BYTES`, `DOGRAH_TRANSCRIPT_MAX_LINES`.

**Kill switch:** `curl -X POST -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' -d '{"reason":"…"}' $API/api/v1/admin/kill-switch`
(`DELETE` to resume, `GET` for status). If the API itself is unreachable: `redis-cli SET outbound:kill_switch '{"enabled_by":"ops"}'`
(`DEL` to clear), or set `OUTBOUND_KILL_SWITCH=true` and restart workers. Workers log `outbound_blocked_not_reading_queue` once a minute while frozen.

## 18. Incident response notes

* **Suspected token/credential leak:** rotate `JWT_SIGNING_KEY` (invalidates every token) and the affected password; check `auth.login_succeeded`
  and `authz.denied` audit rows by `request_id`.
* **Webhook secret leak:** rotate `DOGRAH_WEBHOOK_SECRET` in both places; review `webhook.auth_failed` and `webhook.duplicate`.
* **Runaway/abusive calling:** engage the kill switch first, then investigate; queued jobs are preserved.
* **Redis outage:** outbound stops (fail closed) and enqueue/import return 503; webhooks keep being accepted and recorded.
* **Boot refusal:** the error names each offending field; fix the environment — do not lower the checks.
* Correlate any report with the `X-Request-ID` the client received.

## Validation summary (sandbox, not production)

See the PR description for the exact results. Performance figures are in-process, single-machine, and measure
overhead only; **no 100K-contact throughput claim is made.**
