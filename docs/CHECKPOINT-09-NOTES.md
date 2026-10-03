# Checkpoint 09 — Live Dograh + Production Hardening — implementation notes

This checkpoint hardens the Checkpoint 08 Dograh integration and the
surrounding system for production operation. It does **not** rewrite
CP08, add a second retry engine, or touch the frontend. Everything
below is additive on top of the existing architecture, reusing
existing services/models wherever one already fit.

Every claim below is tagged **VERIFIED** (proven by an automated test
against real Postgres/Redis, or confirmed by reading the actual
code/docs it's based on) or **UNVERIFIED** (believed correct, not
independently proven — most commonly because no live Dograh instance
was reachable in this environment). Dograh-side configuration vs.
this backend's own configuration is also called out explicitly where
relevant, matching the distinction CP08's notes already established.

## 1. The most important correction: call lifecycle (§2)

**VERIFIED.** Checkpoint 08 marked `CallAttempt.state = CONNECTED` and
`Contact.status = IN_CONVERSATION` the moment Dograh's trigger
endpoint returned success. That's wrong: a successful trigger means
**Dograh accepted the job**, not that a phone connected — "provider
acceptance != phone connection" is CP09's own framing, and it's
correct.

`app/services/queue/dialer_worker.py::_place_call_via_dograh` no
longer touches `attempt.state` or `contact.status` on trigger success
at all. They stay at whatever `_dial` already set them to before this
function was called (`INITIATED` / `DIALING`). The **only** place this
integration ever learns whether a Dograh-routed call actually
connected is the completion webhook — there is no separate "call
answered" signal in Dograh's public API (confirmed: no such endpoint
appears in Dograh's published docs or SDK, per Checkpoint 08's own
research). Because of this, `app/services/telephony/
dograh_webhook_service.py`'s classification is now three-way, not
Checkpoint 08's original two-way:

```
call_status keyword match
  -> never connected   (no_answer/busy/invalid_number/rejected/unreachable)
       -> CallAttemptState.FAILED_TO_CONNECT
       -> RecoveryManager.handle_disconnect(..., never_connected=True)
  -> dropped mid-call   (error/timeout/disconnect/drop/technical)
       -> CallAttemptState.DROPPED_MID_CALL
       -> RecoveryManager.handle_disconnect(..., never_connected=False)
  -> else -> ended normally
       -> CallAttemptState.ENDED_NORMALLY
       -> enqueue_call_analysis (CP06, unchanged)
```

A never-connected call gets no `ConversationSession` (there was no
conversation) — **VERIFIED**
(`test_never_connected_call_gets_no_conversation_session`).

Every one of these keyword buckets is a documented heuristic, not an
exhaustive mapping — Dograh's own `call-dispositions.mdx` confirms
`call_status` is "the observed reason the call ended" with no fixed,
published enum. See §2.1 below for the exact keyword lists and how to
extend them.

### 1.1 Known gap in this classification

> **Updated by the post-merge validation pass — see §19.** Unrecognized `call_status` values are no longer treated as a connected conversation; the three-way split now has an explicit fourth, conservative bucket.

A bare `"fail"` is deliberately **not** a dropped-mid-call keyword —
it's ambiguous between "never connected" and "dropped mid-call"
without more context, so only the more specific technical-failure
words (`error`, `timeout`, `disconnect`, `drop`, `technical`) trigger
that bucket. If your workflow's actual `call_status` values don't fit
this split, extend the keyword tuples in `dograh_webhook_service.py` —
this is intentionally a small, visible, editable heuristic rather than
a hardcoded enum migration, because Dograh doesn't publish one to
migrate against. **UNVERIFIED** against a real Dograh instance's
actual `call_status` vocabulary.

## 2. Dograh adapter hardening (§1)

### 2.1 Production trigger (§1.1)

**VERIFIED** (code review + existing contract tests). `DOGRAH_TRIGGER_MODE`
already existed from Checkpoint 08 (`test` vs. `production`, switching
between `/api/v1/public/agent/test/{uuid}` and
`/api/v1/public/agent/{uuid}`). This checkpoint adds a startup-time
guard: `Settings._validate_production_config` now refuses to boot with
`ENVIRONMENT=production` and `DOGRAH_TRIGGER_MODE=test` — the exact
"never silently fall back to a test endpoint for production" rule
from §1.1. No new endpoint behavior was invented; this only prevents
the already-documented test endpoint from being used by accident.

### 2.2 Provider error taxonomy (§1.4)

**VERIFIED.** `app/services/telephony/dograh_client.py::DograhErrorCategory`:
`timeout`, `connection_error`, `authentication_error`,
`validation_error`, `rate_limited`, `provider_unavailable`,
`provider_rejected`, `ambiguous_request`, `unknown_provider_error`.
Classified once, inside the client — callers (the dialer worker) only
ever branch on `exc.category` / `exc.is_ambiguous`, never on raw HTTP
status codes or exception types. 14 tests
(`tests/test_dograh_client.py`) cover every category.

### 2.3 Bounded, phase-specific timeouts (§1.2)

**VERIFIED.** `DOGRAH_CONNECT_TIMEOUT_SECONDS` (default 5s) and
`DOGRAH_READ_TIMEOUT_SECONDS` (default 15s) replace the single flat
timeout Checkpoint 08 shipped with. httpx's own exception hierarchy is
used to tell *which phase* timed out:

- `ConnectTimeout` / `PoolTimeout` → `CONNECTION_ERROR` — the request
  definitely never reached Dograh.
- `ReadTimeout` / `WriteTimeout` → `AMBIGUOUS_REQUEST` — the request
  may have been sent (and even processed) before the response was
  lost.
- Any other/future `TimeoutException` subtype → generic `TIMEOUT`,
  also treated as ambiguous (fail safe toward "might have happened"
  rather than assume it didn't).

### 2.4 Ambiguous call creation (§1.3)

> **Updated by the post-merge validation pass — see §19.** The statement below that no Dograh lookup capability exists is **superseded**: Dograh's published OpenAPI documents an org-wide run listing that carries `initial_context`, and the dialer now uses it to adopt an already-created run. Verified against the docs only, not a live instance.

**VERIFIED** the behavior; **UNVERIFIED/honestly limited** the
reconciliation story — read on.

An ambiguous trigger failure is tagged with its own `CallEvent`
(`DOGRAH_TRIGGER_AMBIGUOUS`, distinct from `DOGRAH_TRIGGER_FAILED`) and
routed through the exact same `RecoveryManager` never-connected path
as any other trigger failure — **never an immediate re-trigger**
(`test_ambiguous_timeout_is_tagged_distinctly_and_not_retried_immediately`
proves exactly one `CallAttempt` row exists synchronously after an
ambiguous failure, and that `DOGRAH_TRIGGER_AMBIGUOUS` is the event
recorded).

What this checkpoint does **not** do, honestly: §1.3 asks for
reconciliation against Dograh ("find the existing run, or safely
conclude none exists") before any retry. No Dograh API capability for
"list/query workflow runs by an external correlation key" was found in
its published docs or SDK during Checkpoint 08's own research (the SDK
exposes generic workflow CRUD and a `test_phone_call` trigger, not a
runs-by-correlation-key lookup). Inventing one would violate CP09's
own rule #3 ("do not invent Dograh API behavior"). So the actual
safeguard against a duplicate call here is **RecoveryManager's
existing backoff** (30s for retry #1, 10 minutes for retry #2) — not
an active reconciliation step. This is a real, documented limitation,
not a solved problem: if Dograh's trigger genuinely succeeded despite
our client timing out on the response, a scheduled retry will dial the
contact a second time. Mitigating this fully would require either a
verified Dograh reconciliation API (not found) or an idempotency-key
contract Dograh's trigger endpoint doesn't document accepting.

## 3. Admission control + circuit breakers (§5, §7)

**VERIFIED.** A Dograh-routed call was previously admitted and
circuit-broken under the *native* mock provider's key
(`provider.name`), meaning it shared concurrency/CPS/circuit-breaker
buckets with an entirely different dependency and had no real
protection of its own. `_process` now computes
`provider_name = "dograh" if calling_engine == "dograh" else
provider.name` for both `admission.try_admit`/`release` calls, and
`_place_call_via_dograh` records success/failure on its own
`CircuitBreaker(redis, "dograh")`. `AdmissionController.try_admit`
already constructs its own internal circuit-breaker check per
`provider_name`, so this one key change makes admission control
automatically respect the Dograh breaker's open/closed state with zero
new abstractions (`test_dograh_circuit_breaker_is_independent_of_native_provider`).

No new circuit breaker *implementation* — the existing
`CircuitBreaker`'s TTL-based recovery already functions as a
single-probe half-open mechanism (the `OPEN` key expires after
`CIRCUIT_BREAKER_OPEN_SECONDS`; the next call is the implicit probe —
success clears the failure count, failure reopens immediately). This
is functionally equivalent to a named `CLOSED -> OPEN -> HALF_OPEN ->
CLOSED` state machine without a third state to maintain.
`tests/test_circuit_breaker.py` proves the full cycle, including the
failed-probe-reopens-immediately case.

Postgres/Redis themselves deliberately do **not** get circuit
breakers: §7 says "only protect actual external dependency
boundaries," and a circuit breaker in front of your own primary
database doesn't provide a meaningful protection the existing
connection-error propagation doesn't already give you — every DB call
already fails fast on a connection error, and "opening the circuit" on
your own source of truth doesn't have a sensible fallback behavior the
way failing over away from a flaky third-party API does.

## 4. Queue hardening (§4)

### 4.1–4.2 Durable jobs, claim/lease

**Unchanged, already correct** — Redis Streams consumer groups
(`RedisStreamQueue`, Checkpoint 03) already provide durable jobs
(survive process restart) and safe claiming (`XREADGROUP` gives
exactly one consumer ownership of a given message).

### 4.3 Stale job recovery — real bug found and fixed

**VERIFIED.** `app/worker.py` called `queue.reclaim_stale(...)`
(`XAUTOCLAIM`, transferring ownership of a stale pending message) but
never re-drove processing on what it returned — only logged the count.
This is the exact same unexercised gap Checkpoint 06 found and fixed
for the analysis queue's reclaim loop, now found and fixed here too: a
contact whose dialer worker crashed after claiming (but before
dialing) its job would sit stuck forever, its message merely
reassigned to a new consumer and never actually executed.

`dialer_worker.py`'s private `_process` is now the public
`process_claimed_job`, used both by `process_one_job` (a fresh
delivery) and directly by `app/worker.py`'s reclaim loop for each
reclaimed job. No duplicate-call risk: `process_claimed_job` re-checks
DB state via the existing `DialEligibilityService` before dialing
(already-existing Checkpoint 03 behavior), so a contact already
handled through a different path, or one that became ineligible in the
meantime, is a safe no-op.
`tests/test_dialer_stale_job_recovery.py` proves both: a stale job is
actually dialed once reclaimed, and a reclaimed job for a now-paused
campaign creates no `CallAttempt` at all.

### 4.4 Retry/backoff

**Unchanged, verified by inspection.** `RecoveryManager` remains the
only owner of retry decisions — nothing in this checkpoint adds a
second retry policy. The Dograh webhook's never-connected and
dropped-mid-call branches call the exact same
`RecoveryManager.handle_disconnect` the native path already uses.

### 4.5 Dead-letter handling

**Not implemented — documented gap.** The dialer queue has no explicit
dead-letter table. In practice, a `CallAttempt` that exhausts
`RecoveryManager`'s retry policy already reaches a terminal,
queryable, auditable state (`FAILED_TO_CONNECT`/`DROPPED_MID_CALL`
with `COMPLETED_PARTIAL` contact status) in Postgres — the durable
source of truth already captures "this contact's calling is done and
it didn't succeed," which is the operationally important part of a
DLQ. A literal separate dead-letter *table* for jobs that fail at the
queue-processing level (as opposed to the dial itself failing) wasn't
added, since Checkpoint 03's existing admission/eligibility checks
already prevent most queue-level processing failures from being
silent, and adding a new table is schema scope-creep CP09 explicitly
asks to avoid unless "actually necessary." If malformed/poison
messages in the dial queue turn out to be a real operational problem,
a DLQ table is the natural next addition.

### 4.6 ACK safety

> **Updated by the post-merge validation pass — see §19.** This section's claim was **not true of the code as merged**: the ack ran in a `finally` before the worker's commit. Fixed — ack now follows a durable commit.

**Unchanged, verified by inspection** — `queue.ack(message_id)` in both
the dialer and analysis workers already only happens after the
relevant DB state has been flushed, matching §4.6 exactly as it
already did before this checkpoint.

## 5. Webhook hardening (§3)

### 5.1 Authentication (§3.1)

**Unchanged, already correct** — `_verify_secret` already uses
`secrets.compare_digest` (constant-time) and accepts either a Bearer
token or an `X-API-Key` header, whichever the Dograh Webhook node is
configured with. Fails closed (401) on any mismatch or missing header.

### 5.2 Payload validation (§3.2)

**VERIFIED.** `DograhWebhookPayload` now bounds every free-text field's
length (`call_attempt_id` ≤64 chars, `call_status`/`call_disposition`/
`mapped_call_disposition` ≤500, `recording_url`/`transcript_url`
≤2048), requires `recording_url`/`transcript_url` to be `http(s)` URLs
when present, and tolerates unknown extra fields (Dograh's
`payload_template` is user-configurable and may grow new variables
later — `extra="ignore"`, not `"forbid"`). 5 tests cover rejection
(oversized fields, non-http URL) and tolerance (unknown fields,
missing optional fields).

### 5.3 Replay protection / idempotency (§3.3, §3.4)

> **Updated by the post-merge validation pass — see §19.** The concurrent-duplicate race documented here is **fixed** (claim-first in a SAVEPOINT; the unique constraint remains the authority) and covered by 2/8/16-way real-thread tests.

**VERIFIED.** Reuses the existing `ProcessedEvent` model and the exact
idiom already used by the Twilio-style telephony webhook
(`app/services/webhook_service.py`) — not a new mechanism. Event
identity is `f"dograh:{workflow_run_id}"` (Dograh's own per-triggered-
call unique identifier), falling back to `f"dograh:{call_attempt_id}"`
for the rare case a run ID wasn't captured — checked **before** any
state mutation. The attempt-state terminal check from Checkpoint 08
(`attempt.state in (ENDED_NORMALLY, DROPPED_MID_CALL, FAILED_TO_CONNECT)`)
remains as defense in depth alongside it.

Dograh does not publish a cryptographic webhook signature mechanism
(confirmed: its Webhook node's own auth options are Bearer token /
API key / basic auth / custom header against a stored credential, not
a signed payload) — per §3.3's own instruction not to invent one where
none exists, this integration relies on the authenticated shared
secret + event-identity deduplication above, not a signature or a
timestamp-based replay window. **Documented limitation**, not silently
assumed equivalent to signature verification.

**Known gap**: a theoretical race exists if two deliveries for the
same event arrive concurrently enough to both pass the
`ProcessedEvent` existence check before either commits — the second to
commit would hit the column's database-level `unique=True` constraint
and surface as a 500 rather than a graceful "already processed." This
exact pattern (check-then-insert, no explicit `IntegrityError` catch)
already exists in the pre-existing Twilio webhook service this
checkpoint matched for consistency, so it's a pre-existing, accepted
risk shape in this codebase rather than a new one introduced here —
noted, not silently carried forward unremarked.

### 5.4 Transactional persistence

**Unchanged, already correct** — webhook processing validates, loads,
transitions, and flushes before the route returns; `get_db`'s
dependency commits only after the handler returns successfully and
rolls back on any exception (including a raised `HTTPException`),
matching §3.5's pipeline exactly.

## 6. Admission re-check before every dial (§5)

**Unchanged, already correct, verified by inspection** — Checkpoint
03's `_dial` already re-runs `DialEligibilityService.check(...)`
(suppression, campaign active, contact status, retry policy) freshly
from the database immediately before `_place_call`, for both the
native and Dograh paths (the branch happens *inside* `_place_call`,
after this check). `Settings._validate_production_config` additionally
confirms Dograh configuration (API key, trigger UUID, trigger mode)
at process *startup*, rather than discovering a misconfiguration on
the first dial — the "Dograh configuration" item from §5's re-check
list.

## 7. Recovery (§6)

**Unchanged, already correct.** `RecoveryManager` remains the sole
owner of every retry/reconnect decision touched by this checkpoint —
the Dograh trigger-failure path, the ambiguous-timeout path, and both
new webhook branches (never-connected, dropped-mid-call) all call
`RecoveryManager.handle_disconnect` or the existing
`_handle_never_connected_failure` helper, never a parallel decision.
Canonical policy (`max_retries=2`, 30s/10min spacing, no retry on
explicit rejection or suppression) is untouched.

## 8. Security (§8)

- **8.1 Secrets**: `Settings._validate_production_config` (new) fails
  startup in `ENVIRONMENT=production` if `JWT_SIGNING_KEY`,
  `ADMIN_PASSWORD`, `OPERATOR_PASSWORD`, `TELEPHONY_WEBHOOK_SECRET`, or
  `DOGRAH_WEBHOOK_SECRET` are still at their dev-only-insecure
  defaults, or if `CALLING_ENGINE=dograh` without `DOGRAH_API_KEY`/
  `DOGRAH_TRIGGER_UUID` set. **VERIFIED** directly
  (`test_core_config.py`-style inline test in this checkpoint's first
  commit).
- **8.2 Webhook security**: see §5.1/5.3 above.
- **8.3 PII-safe logging**: audited every `logger.*` call this
  checkpoint touches or added (`dialer_worker.py`,
  `dograh_webhook_service.py`, `dograh_client.py`) — none log a raw
  phone number, transcript content, API key, or token; all use IDs
  (`attempt_id`, `contact_id`, `campaign_id`, `trace_id`,
  `workflow_run_id`). Confirmed via repo-wide grep that no `logger.*`
  call anywhere references `phone_number`/`normalized_phone_number`.
  Masking utility (`mask_phone_number`, Checkpoint 07) remains
  available for any future log line that does need to reference a
  number.
- **8.4 RBAC**: unchanged — Checkpoint 07's admin/operator boundary
  (`require_admin`/`require_role`) wasn't touched by this checkpoint.
- **8.5 Rate limiting**: new, minimal, Redis-backed fixed-window
  limiter (`app/core/rate_limit.py`) applied to admin login
  (`LOGIN_RATE_LIMIT_PER_MINUTE`, default 10/min) and the Dograh
  webhook (`WEBHOOK_RATE_LIMIT_PER_MINUTE`, default 120/min). Fails
  open on a Redis error (an unreachable rate limiter must never itself
  take the endpoint down, per §12). Internal worker loops never call
  this. **VERIFIED** (`test_rate_limiting.py`).
- **8.6 Audit events**: the Dograh webhook's three outcome branches
  (never-connected, ended-normally, dropped-mid-call) each now call
  `record_audit_event` (the existing audit service, unchanged) in
  addition to the `CallEvent` row — matching the "all state
  transitions must be auditable" instruction.

## 9. Observability (§9)

- **Correlation IDs**: `attempt_id`, `contact_id`, `campaign_id`, and
  `trace_id` (from `DialJob`) are attached to every Dograh-path log
  line via `log_extra`; `workflow_run_id` is added once known.
- **Metrics**: no new metrics framework was introduced (none existed
  before this checkpoint beyond structured `logger.info`/`.warning`
  calls with `extra=`) — the large metrics list in §9 (CALLS_INITIATED,
  QUEUE_DEPTH, DOGRAH_LATENCY, etc.) is **not implemented** as counters/
  gauges in this checkpoint. What exists today: the admin dashboard's
  `/api/v1/admin/dashboard/overview` and `/system` endpoints
  (Checkpoint 07) already compute several of these on demand from
  Postgres/Redis (call counts by state, queue pending depth, analysis
  failure rate). A real metrics/counters backend (Prometheus-style)
  would be new infrastructure this checkpoint's own "do not introduce
  a massive monitoring framework" instruction argues against adding
  speculatively — documented as a gap, not silently skipped.
- **Health endpoints**: `GET /health` (liveness, unchanged) and new
  `GET /ready` (readiness — Postgres + Redis only, 503 if either is
  down, no secrets in the response body). See `app/api/routes/health.py`.

## 10. Production configuration (§10)

**VERIFIED.** `Settings._validate_production_config` — see §8.1. Every
variable §10 lists already existed in `Settings`
(`DATABASE_URL`→`PRIMARY_DB_URL`, `REDIS_URL`, `DOGRAH_API_BASE_URL`,
`DOGRAH_API_KEY`, `DOGRAH_TRIGGER_UUID`, `DOGRAH_WEBHOOK_SECRET`,
queue/worker/rate-limit settings) — this checkpoint only added the
startup-time validation, not new variables beyond the rate-limit
settings (§8.5) and the connect/read timeout split (§2.3).

### Docker / graceful shutdown

**Unchanged, verified by inspection.** Both `app/worker.py` and
`app/analysis_worker.py` already install `SIGTERM`/`SIGINT` handlers
that stop claiming new jobs while letting an in-flight job finish
(Checkpoint 05/06) — not modified by this checkpoint. Dockerfiles were
not touched in CP09 (the frontend Dockerfile's `npm ci --legacy-peer-
deps` / `docker-entrypoint.sh` line-ending fixes predate this
checkpoint — see the PR history around CP07).

## 11. Database safety (§11)

**VERIFIED no schema change needed.** `alembic check` reports "No new
upgrade operations detected" after this checkpoint's full diff — every
model this checkpoint relies on (`ProcessedEvent.event_id` unique,
`CallAttempt.provider_call_id` unique index) already existed from
earlier checkpoints with the constraints needed. No migration was
created.

## 12. Redis safety (§12)

**Unchanged, verified by inspection.** Redis is used here exactly as
§12 prescribes: queue/hot state (the dial/analysis streams, unchanged),
locks (consumer-group claiming), rate limiting (new, this checkpoint),
and circuit-breaker state (ephemeral, TTL-backed). No critical business
state was added to Redis by this checkpoint — every state transition
this checkpoint makes is a Postgres write (`CallAttempt`, `Contact`,
`CallEvent`, `AuditLog`, `ProcessedEvent`). A Redis outage during
admission (`try_admit`) already fails the job un-admitted (left
unacked, picked up again later) rather than silently proceeding to
dial — unchanged Checkpoint 03 behavior, re-verified by reading
`AdmissionController.try_admit`, not re-tested in this checkpoint.

## 13. Failure injection (§13)

Of the 20 scenarios listed, status:

| # | Scenario | Status |
|---|---|---|
| 1 | Dograh timeout | **VERIFIED** (`test_dograh_client.py`) |
| 2 | Dograh 4xx | **VERIFIED** |
| 3 | Dograh 5xx | **VERIFIED** |
| 4 | Dograh unavailable (connection error) | **VERIFIED** |
| 5 | Redis unavailable | Partial — rate limiter fail-open **VERIFIED**; queue/admission Redis-down behavior not newly tested (pre-existing, unchanged) |
| 6 | PostgreSQL transient failure | Not tested — would need a DB-layer fault injection harness this checkpoint didn't build |
| 7 | Worker crash | **VERIFIED** (`test_dialer_stale_job_recovery.py`) |
| 8 | Stale lease | **VERIFIED** (same file) |
| 9 | Duplicate webhook | **VERIFIED** (`test_dograh_webhook_hardening.py`, `test_dograh_webhook.py`) |
| 10 | Malformed webhook | **VERIFIED** |
| 11 | Unauthorized webhook | **VERIFIED** (Checkpoint 08, re-run unchanged) |
| 12 | Replayed webhook | **VERIFIED** (ProcessedEvent dedup) |
| 13 | Invalid state transition | **VERIFIED** (terminal-state idempotency) |
| 14 | Ambiguous Dograh request | **VERIFIED** |
| 15 | Provider recovery | **VERIFIED** (`test_circuit_breaker.py`) |
| 16 | Retry storm | Not explicitly burst-tested — bounded by existing `max_retries=2` + 30s/10min backoff, not separately load-tested in this checkpoint |
| 17 | Campaign paused during retry | **VERIFIED** (`test_reclaimed_job_is_not_reprocessed_twice`) |
| 18 | Suppression added while queued | **VERIFIED** — pre-existing `tests/test_dialer_worker.py::test_worker_skips_suppressed_contact_without_dialing` (Checkpoint 03), re-run unchanged, still passing |
| 19 | Opt-out during conversation | **VERIFIED** — pre-existing `tests/test_ai_opt_out.py` (Checkpoint 04), re-run unchanged, still passing |
| 20 | Shutdown during active worker execution | Not tested — existing signal-handler code (Checkpoint 05/06) wasn't exercised with a new test; simulating OS signal delivery mid-loop in a unit test has low value relative to the complexity of building that harness |

## 14. Dograh E2E validation (§14)

**NOT VERIFIED.** No live Dograh instance (self-hosted or cloud) was
reachable in this environment. Everything above is verified against
Dograh's **published documentation and source** (cloned and read
directly, see Checkpoint 08's notes) plus mocked HTTP responses in
this repository's own test suite — never against a running Dograh
deployment. Do not treat this integration as production-ready until a
real controlled test call has been run through it and the checklist in
`docs/CHECKPOINT-08-NOTES.md`'s "Manual setup checklist" has actually
been completed end-to-end once.

## 15. Testing gate results

- Backend tests: **305 passing** (274 before this checkpoint + 31 new).
- `ruff check .`: clean.
- `mypy app/`: clean (130 source files).
- `alembic check`: no new upgrade operations detected (no migration).
- No leaked secrets (manual grep sweep of the diff for `key`/`secret`/
  `password`/`token` patterns — every match is a variable/field name or
  a dev-only-insecure placeholder, confirmed individually).
- No debug `print`/`console.log`/`pdb`/`breakpoint()` in the diff.
- No duplicate retry engine — confirmed by code review: every retry
  decision in this checkpoint's new/changed code calls
  `RecoveryManager`.
- No frontend changes (confirmed: diff touches only `backend/` and
  `docs/`).
- No unnecessary schema changes (§11).
- CP00–CP08 regression: all 274 pre-existing tests still pass
  unchanged.

## 16. Performance / load safety (§16)

**Not load-tested in this checkpoint.** The stated baseline (100,000
contacts/month, 35% connect rate, 3.5 min average connected-call
duration) was not exercised with a dedicated load-testing pass here —
Checkpoint 07's own performance verification (~1,000 contacts, ~3,000
call attempts, 1,000 analyses against the admin dashboard's read
endpoints) is the closest existing data point, and it doesn't cover
the dial/webhook write path this checkpoint hardened. Scaling the
existing architecture's concurrency/CPS limits
(`AdmissionController`'s global/campaign/provider limits, already
configurable via `Settings`) to the stated baseline is believed
feasible given the existing design (Redis Streams consumer groups
scale horizontally by adding worker processes, matching the project's
established pattern since Checkpoint 03) but this belief is
**UNVERIFIED** by an actual load test in this checkpoint.

## 17. Known limitations (consolidated)

> **Updated by the post-merge validation pass — see §19.** Several items below were closed or re-scoped by §19; §19.6 is the current list.

- Dograh E2E not verified against a live instance (§14).
- Ambiguous-timeout retry safety relies on `RecoveryManager`'s backoff
  window, not active provider reconciliation — no verified Dograh API
  for that exists (§2.4).
- `call_status` classification is a documented keyword heuristic, not
  an exhaustive mapping (§1.1).
- No dead-letter table for the dial queue (§4.5) — terminal
  `CallAttempt` state in Postgres already captures the operationally
  important part.
- No metrics/counters backend — structured logs + the existing admin
  dashboard's on-demand aggregates are what exist today (§9).
- Theoretical concurrent-duplicate-webhook race at the database
  constraint level, consistent with a pre-existing pattern elsewhere
  in this codebase (§5.3).
- No dedicated load test at the 100K-contacts/month baseline (§16).
- Redis-unavailable / Postgres-transient-failure / retry-storm /
  shutdown-mid-execution failure scenarios were not newly tested in
  this checkpoint (§13, rows 5/6/16/20).

## 18. Env vars added in this checkpoint

```
DOGRAH_CONNECT_TIMEOUT_SECONDS=5      # replaces DOGRAH_REQUEST_TIMEOUT_SECONDS
DOGRAH_READ_TIMEOUT_SECONDS=15        # replaces DOGRAH_REQUEST_TIMEOUT_SECONDS
LOGIN_RATE_LIMIT_PER_MINUTE=10
WEBHOOK_RATE_LIMIT_PER_MINUTE=120
```

`ENVIRONMENT=production` now also requires (enforced at startup, see
§8.1/§10): `JWT_SIGNING_KEY`, `ADMIN_PASSWORD`, `OPERATOR_PASSWORD`,
`TELEPHONY_WEBHOOK_SECRET`, `DOGRAH_WEBHOOK_SECRET` all changed from
their dev-only defaults, and, when `CALLING_ENGINE=dograh`:
`DOGRAH_API_KEY`, `DOGRAH_TRIGGER_UUID` set and
`DOGRAH_TRIGGER_MODE=production`.


---

## 19. Post-merge validation pass

PR #28 (and #29, `develop` → `main`) were already merged when this pass ran, so
this work lives on a follow-up branch. Nothing here was verified against a live
Dograh instance. Status keys: **VERIFIED** (executed, evidence cited) ·
**UNVERIFIED** · **BLOCKED BY ENVIRONMENT** · **DOCUMENTED LIMITATION** ·
**DEFERRED**.

### 19.1 Evidence table

| Area | Status | Evidence |
|------|--------|----------|
| Dograh lifecycle | VERIFIED (mocked provider) | existing CP09 tests; full suite 393 passed twice |
| Error taxonomy | VERIFIED | `test_dograh_client.py`, `test_dograh_reconciliation_client.py` |
| Ambiguous request | PARTIAL | first lookup (`test_failure_injection.py`, 8 cases) and a second lookup immediately before the retry (`test_reconciliation_retry_gate.py`, 27 cases incl. 2/8-worker races). Docs-only: Dograh's published OpenAPI, not a live instance. Residual window in §19.4 |
| Queue recovery / ACK safety | VERIFIED | ack-after-commit; PG-transient, commit-after-trigger and orphaned-claim tests; fail against the pre-fix dialer |
| Admission / circuit breaker | VERIFIED | Redis-down-in-admission fails closed; open circuit stops traffic then resumes |
| Webhook auth | VERIFIED (unchanged) | CP09 tests |
| Webhook replay / concurrency | VERIFIED | 2/8/16-way real-thread tests (6/6 fail without the fix); 500/500 replays idempotent at 30K |
| Failure injection | VERIFIED, injected faults | PG error injected as `OperationalError` (no real PG outage); Redis outage via a client that raises `ConnectionError`; retry storm; real SIGTERM against `worker.run()` |
| Live Dograh E2E | UNVERIFIED — BLOCKED BY ENVIRONMENT | no Dograh instance, API key, webhook secret or authorized number available |
| 100K load validation | PARTIAL | dial stage at 100,000 jobs; webhook/DB-write/recovery stages at 30,000 (§19.5) |
| Security | VERIFIED (static) | diff scans: no secrets, no debug artifacts, no PII in new log lines, no frontend changes |
| Production readiness | **BLOCKED** | live E2E and the `call_status` vocabulary (§19.6) are unverified |

### 19.2 Defects found and fixed

1. **ACK before commit (§4.6 was untrue as merged).** `process_claimed_job` acked
   in a `finally`, before the worker committed: a transient DB error lost the job,
   and a failed commit after a successful trigger left a placed call with no durable
   record. Now: commit → ack; any failure rolls back and leaves the message pending.
2. **Duplicate dial on re-delivery.** For Dograh (no idempotency key) an existing
   attempt without a `provider_call_id` — i.e. every failed trigger — was re-triggered.
   Now any existing attempt blocks a re-trigger. The claim is committed *before* the
   trigger; an unresolved claim older than the request timeouts is treated as an
   ambiguous trigger and goes through RecoveryManager; a younger one is left unacked
   (it may still be in flight).
3. **Concurrent duplicate webhook → 500.** The event is now claimed first in a
   SAVEPOINT; the unique constraint stays the authority and a conflict is an
   idempotent no-op. Applied to the telephony webhook too (same race).
4. **Unsafe `call_status` fallback.** Unknown values were silently "ended normally".
   Normal completion is now matched on whole tokens (`incomplete` ≠ `complete`);
   anything unrecognized is a never-connected provider error — never a connected
   conversation — routed only through RecoveryManager and audited.
5. **Test suite not repeatable.** A second run on unmodified `main` failed 5 tests
   (committed rows leaked). `conftest.py` now truncates the test DB once per session.

### 19.3 Ambiguous-request reconciliation (supersedes §2.4)

Dograh documents `GET /api/v1/organizations/usage/runs` (`start_date`/`end_date`
bound `created_at`; `limit` ≤ 100; each run carries `initial_context`). There is no
server-side filter on an arbitrary `initial_context` key, so
`DograhClient.find_runs_for_attempt` matches our round-tripped `call_attempt_id`
client-side over a bounded window (5 pages × 100). A window larger than that bound
raises instead of returning "none". Outcomes: exactly one run → adopted (state stays
`INITIATED`, never `CONNECTED`; the webhook decides); several → never chosen, never
retried, audited; none / provider error / timeout → unchanged RecoveryManager
backoff. Dograh idempotency keys, HMAC signatures and a `call_status` enum remain
undocumented and are not assumed.

### 19.4 Second reconciliation before the retry (narrows the race; does not close it)

The first lookup cannot prove absence: a timed-out trigger can still land after
it. So when a retry that RecoveryManager has already approved is about to be
dialed, `_second_reconciliation_gate` (dialer worker, after the eligibility
check and before the attempt claim) looks the previous attempt up once more.
It runs **only** when the previous attempt ended as an unresolved
`DOGRAH_TRIGGER_AMBIGUOUS` with no run id — never for normal calls, completed
calls, definite provider failures, or unrelated webhooks.

| Second lookup | Action |
|---|---|
| exactly one run | adopt it on the previous attempt, no new trigger, audited |
| several runs | choose none, trigger nothing, audited (same handling as the first lookup) |
| none | the approved retry proceeds; no third lookup; retry count/backoff untouched |
| timeout / API error / misconfigured | fail closed: no dial; job left **unacked** so a redelivery re-checks; audited |

Safety properties, each with a test:

- **RecoveryManager stays the only retry owner.** The gate adds no queue,
  scheduler or loop; it can only block or adopt an already-due retry.
- **Eligibility is never bypassed:** suppression, paused campaign and a closed
  calling window refuse the job *before* any lookup.
- **Concurrency:** the `(contact, attempt_number)` claim already bounds triggers
  to one. The gate adds a short row lock on the previous attempt, taken only
  after the HTTP lookup, so that concurrent workers adopt once rather than N times.
  2 and 8 workers on one retry produce exactly one trigger (or one adoption).
- **The completion webhook is never lost.** An unresolved ambiguous attempt is
  `FAILED_TO_CONNECT` with no run id, and the webhook used to drop terminal
  attempts *before* claiming the event. If the run's completion webhook beat
  the second lookup it was silently discarded, and a later adoption would then
  reopen an attempt waiting for a webhook that had already come. The webhook now
  treats an unresolved ambiguous attempt as provisional: it adopts the run id,
  reopens the attempt and processes normally (one `ConversationSession`, one
  analysis job; replays are no-ops). A definite failure stays final. Detection
  and reopen are one shared helper (`dograh_reconciliation.py`) used by both
  the gate and the webhook.
- **Idempotent:** once a run is adopted, a redelivered retry sees the previous
  attempt active and stops; no new lookup, trigger or audit.
- **Deliberate state exception:** adopting reopens the previous attempt
  (`FAILED_TO_CONNECT` → `INITIATED`, contact → `DIALING`). The webhook refuses
  terminal attempts, so without this the real call's outcome would be dropped.
  It is limited to an ambiguous failure with no run id and exactly one match.
  The attempt is never marked `CONNECTED`, and no `ConversationSession` is created.
- **Observability:** logs and audit rows carry only campaign/contact/attempt/job
  ids, run ids and error types (`reconciliation_first_lookup`,
  `_second_lookup`, `_run_adopted`, `_no_run`, `_multiple_runs`,
  `_lookup_timeout`, `_lookup_error`, `_retry_allowed`, `_retry_blocked`).

**What this does not guarantee.** The second reconciliation reduces the race
window but cannot provide an absolute distributed guarantee that a provider
request will not complete immediately after the final lookup. The real
protection is the combination of provider reconciliation, attempt claims,
database constraints, RecoveryManager and idempotent processing. Not
verified against a live Dograh instance.

### 19.5 Synthetic load test (backend capacity only)

Harness: `backend/scripts/load_test_backend.py`; raw results in `docs/load-test/`.
Fake Dograh, real PostgreSQL and Redis, real uvicorn for webhooks. Environment:
**1 CPU / 4 GB shared by the load generator, uvicorn, PostgreSQL and Redis**, so
figures are pessimistic. They are BACKEND capacity — not real Dograh capacity and
not telephony/carrier capacity; no real call was placed.

| Stage | 100,000 contacts | 30,000 contacts |
|---|---|---|
| Contact load | 20,674/s | — |
| Queue ingestion | 11,499 jobs/s | — |
| Admission | 2,619 ops/s (p50 0.34 / p95 0.56 / p99 0.79 ms) | — |
| Worker claim | 7,879/s (p50 0.11 / p95 0.21 / p99 0.27 ms) | — |
| Dial pipeline (8 workers) | 92.9 jobs/s; p50 82 / p95 111 / p99 139 ms; util 99%; ~372 DB writes/s | 91.4 jobs/s; p50 84 / p95 113 / p99 139 ms |
| Failed jobs / error rate | 0 / 0% | 0 / 0% |
| Duplicate attempts / runs / dials | 0 / 0 / 0 | 0 / 0 / 0 |
| Webhook ingestion (8 threads) | not completed | 81.3 events/s; p50 95 / p95 134 / p99 184 ms; ~546 DB writes/s; 0 errors |
| Replay under load | — | 500/500 `already_processed` |
| Outcome mix | — | 10,500 analysis enqueued (35%), 19,500 retries scheduled (65%) |
| Recovery dispatch drain | — | 19,500 jobs in 93 s (209/s) |

A first 100K run lost its webhook stage to an environment reset before results were
saved; the harness now persists each stage as it completes. "Max queue depth" /
"oldest age" in the dial stage are the pre-loaded backlog draining, not instability.
Retry boundedness (max 3 attempts, 30 s / 10 min) is proven by the 30-contact storm
test, not at 100K. Context only: 100K contacts/month averages ≈ 0.04 dials/s.

### 19.6 Current limitations / pre-go-live gate

- **BLOCKED BY ENVIRONMENT:** live Dograh E2E (tests A–F) not run.
- **UNVERIFIED:** the normal-completion token list (`hangup, completed, complete,
  finished, success, successful`) against real Dograh output. If it misses real
  values, completed calls would be retried (≤ 2×). Confirm on the first live calls via
  the `classification: unrecognized` audit field.
- **DOCUMENTED:** webhook rate limit is per source IP (default 120/min) and Dograh does
  not retry non-200 by default — a burst above the limit drops completions. Size it
  (`WEBHOOK_RATE_LIMIT_PER_MINUTE`) before scaling concurrency.
- **DOCUMENTED:** recovery dispatch is 50 jobs per call, one call per 10 worker
  iterations (≈ 5/s when idle, derived from code).
- **DOCUMENTED (pre-existing):** calling window is UTC wall-clock with no per-campaign
  timezone (10:00–18:00 UTC = 15:30–23:30 IST); needs a schema change.
- An orphaned claim consumes one retry: double-dial avoidance is prioritised over the
  attempt budget.
- **DOCUMENTED:** if the second lookup keeps failing (e.g. an API key that cannot
  list runs), ambiguous retries are held unacked rather than dialed blind. This is
  fail-closed by design, repeats one audited lookup per redelivery, and is not
  auto-terminalized; watch `dograh.reconciliation_lookup_*` audit events.
- **DEFERRED:** stale `INITIATED` sweeper for lost webhooks.
