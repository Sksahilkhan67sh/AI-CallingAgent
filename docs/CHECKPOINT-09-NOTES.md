# Checkpoint 09 — Live Dograh + Production Hardening

Builds incrementally on CP08. No frontend change, no schema change (no
migration), no second retry engine. `RecoveryManager` remains the only owner
of retry/reconnect decisions; PostgreSQL remains the source of truth; Redis
holds queue, hot state, locks, rate limits and counters only.

Legend: **VERIFIED** = confirmed from Dograh's source/docs or by a test in this
repo. **UNVERIFIED** = assumed/not testable here. **DOGRAH-SIDE** = configured in
Dograh. **OUR-BACKEND** = configured in this repo's environment.

---

## 1. What CP09 found in CP08 and fixed

| Defect in CP08 | Risk | CP09 fix |
|---|---|---|
| Call marked `CONNECTED`/`IN_CONVERSATION` as soon as Dograh *accepted* the trigger | Wrong state; analysis/retry logic acting on calls that never connected | Attempt stays `INITIATED`/contact `DIALING` until a verified completion. Provider acceptance ≠ connection |
| Any timeout → `FAILED_TO_CONNECT` → RecoveryManager retry | **Duplicate calls** when the request had actually reached Dograh | Ambiguity classification + intent marker + reconciler (§4) |
| Job `ack`ed in `finally` before the DB commit | Crash after ack = call placed, state lost | Commit first, ack after (§5) |
| Reclaimed stale jobs were claimed then discarded | Jobs of a dead worker stuck forever | Reclaimed jobs go through the same pipeline (§5) |
| Concurrency slot released right after trigger | Concurrency limits meaningless for multi-minute calls | Dograh concurrency counted from PostgreSQL (§6) |
| No webhook dedupe marker, no correlation check, no URL guard | Replays, cross-run mutation, SSRF via `transcript_url` (also logged) | §3 |
| `no-answer`/`busy` webhooks classified as `ENDED_NORMALLY` | Unanswered calls counted as completed and analysed | `classify()` (§3.4) |
| No half-open circuit state, counter never expires | Retry storm after an outage | §7 |
| Worker hot-looped on Redis/DB errors; no JSON logs/PII masking | Log flood, PII in logs | §9 |

## 2. Dograh production adapter — `services/telephony/dograh_client.py`

**VERIFIED from dograh-hq/dograh source/docs:**
- Production trigger: `POST /api/v1/public/agent/{trigger_uuid}`; draft/test trigger is
  `/api/v1/public/agent/test/{uuid}` (never used when `DOGRAH_TRIGGER_MODE=production`).
  Auth `X-API-Key`. Response `{status, workflow_run_id, workflow_run_name}`.
- Trigger returns **429** "Concurrent call limit reached" *before* a run is created.
- Our `initial_context` is merged into the run's `initial_context` (so `call_attempt_id`
  round-trips). Dograh sets `called_number` itself.
- `GET /api/v1/workflow/{workflow_id}/runs/{run_id}` and
  `GET /api/v1/workflow/{workflow_id}/runs` (documented `dateRange` filter, paginated)
  return `is_completed`, `initial_context`, `gathered_context`, `cost_info`.
- The Webhook node has **no signature and no event timestamp**; non-2xx deliveries are
  **not retried** unless `retry_config` is set.
- There is **no mid-call "connected" event.**

**Error taxonomy** (`ProviderErrorKind`): `timeout`, `connection_error`,
`authentication_error`, `validation_error`, `rate_limited`, `provider_unavailable`,
`provider_rejected`, `ambiguous_request`, `unknown_provider_error`. Each
`DograhApiError` carries `.kind` and `.ambiguous`.

| Condition | kind | ambiguous | Handling |
|---|---|---|---|
| ConnectTimeout / ConnectError / PoolTimeout | timeout / connection_error | **no** (request never sent) | `FAILED_TO_CONNECT` → RecoveryManager |
| ReadTimeout / WriteTimeout / RemoteProtocolError | timeout / connection_error | **yes** | reconcile, **no retry** |
| 5xx | provider_unavailable | **yes** | reconcile, **no retry** |
| 2xx without a usable run id | ambiguous_request | **yes** | reconcile, **no retry** |
| 400/404 | provider_rejected | no | RecoveryManager |
| 401/403 | authentication_error | no | RecoveryManager (bounded) + alert (§12) |
| 422 | validation_error | no | RecoveryManager |
| 429 | rate_limited | no (verified) | RecoveryManager; does not count toward the circuit breaker |

Timeouts: `DOGRAH_CONNECT_TIMEOUT_SECONDS` (default 5) and
`DOGRAH_REQUEST_TIMEOUT_SECONDS` (read/write, default 15). Nothing can hang.

## 3. Webhook — `api/routes/dograh_webhook.py`, `dograh_webhook_service.py`

Pipeline: rate limit → authenticate (constant-time, bytes, **fails closed** if the
secret is unset; auth runs *before* body validation) → validate payload (types,
lengths, `http(s)` URLs, non-negative bounded duration; Dograh's `""`/`"None"`
placeholders mean "unknown") → one transaction: `SELECT … FOR UPDATE` on the attempt →
correlation (`provider=="dograh"`, `workflow_run_id` must match the stored run id; adopted
if we never got one) → terminal-state guard → unique `ProcessedEvent("dograh:<attempt_id>")`
in a savepoint → validated transitions → **commit → ACK** → post-commit analysis admission.

- **3.3 Replay protection (limitation, UNVERIFIED-by-design):** Dograh gives no signature
  or trustworthy timestamp, so none was invented. Protection = shared secret + unique
  event marker + run-id correlation + terminal-state immutability. A *valid secret holder*
  can still replay a **first** completion only once; subsequent copies are no-ops. There is
  no time window because a call's age is unbounded and Dograh's `call_time` is the run
  creation time, not delivery time.
- **3.4 Classification** (`call_status` is free text in Dograh): never-connected keywords
  (busy / no-answer / reject / invalid) with no positive duration ⇒ `FAILED_TO_CONNECT`
  with the matching reason (rejected & invalid are never retried by the default policy);
  error keywords with `duration==0` ⇒ `FAILED_TO_CONNECT(provider_error)`; error keywords
  otherwise ⇒ `DROPPED_MID_CALL`; else `ENDED_NORMALLY`. A positive duration always wins.
  **UNVERIFIED:** the exact `call_status` strings a given telephony provider emits — verify
  against your Dograh instance (§15) and extend the keyword tables if needed.
- **Opt-out:** a disposition of `dnc`/`do_not_call`/`opt_out`/`optout`/`stop` creates a
  `Suppression` (via the existing repository), closes the contact, schedules no retry and
  admits no analysis. **DOGRAH-SIDE:** your workflow must emit such a disposition.
- **Transcripts:** fetched after validation with an SSRF guard (public addresses only,
  no redirects, 10 s, 2 MB, URL never logged). Self-hosted storage on a private network:
  list its host in `DOGRAH_TRANSCRIPT_ALLOWED_HOSTS`.

### State model
`CallAttempt.state` (unchanged enums) is separate from reason/outcome columns and from
`CallAnalysis`. All moves go through `services/call_state.transition()`:

```
INITIATED ─► CONNECTED ─► ENDED_NORMALLY | DROPPED_MID_CALL
    └──────► FAILED_TO_CONNECT              (terminal states never move again)
```
Because Dograh sends no mid-call event, a completion that proves a conversation records
`INITIATED→CONNECTED→terminal` as two audited `CALL_STATE_TRANSITION` events.

## 4. Ambiguous call creation

1. Before the HTTP request the attempt row is committed with `provider="dograh"`
   (a `PROVIDER_REQUEST_INTENT` event). Any later redelivery of the job sees that marker
   and **never triggers again** — even after a crash mid-request.
2. An ambiguous result writes `AMBIGUOUS_PROVIDER_STATE`, leaves the attempt `INITIATED`,
   and does **not** call RecoveryManager.
3. The reconciler (worker, every 30 s) acts on attempts older than
   `DOGRAH_RECONCILE_AFTER_SECONDS` (120): with no run id it scans runs created since the
   attempt for our `call_attempt_id`. **Found** → adopt it and continue; **not found** →
   conclude no call was placed → `FAILED_TO_CONNECT(provider_error)` → RecoveryManager
   decides. With a run id it reads the run: completed ⇒ applies the completion exactly as
   the webhook would (covers lost webhooks); still running past
   `DOGRAH_STALE_ATTEMPT_SECONDS` (3600) ⇒ terminalized. Dograh unreachable ⇒ attempt left
   untouched, retried next sweep.
4. **UNVERIFIED:** run listing is assumed to be read-your-writes within the 120 s grace
   period. If Dograh's listing lagged longer, a "not found" could precede a late-appearing
   run, i.e. a theoretical duplicate. The grace period is configurable for this reason.

## 5. Queue / worker contract

`read → admit → load DB state → validate → execute → COMMIT → ack`.
- Jobs are Redis Streams entries (durable with Redis AOF — **deploy Redis with
  `appendonly yes`**). The business record of every call is in PostgreSQL, so a lost job
  can be re-enqueued but a call is never lost or doubled.
- Lease = stream consumer ownership + `QUEUE_RECLAIM_IDLE_MS`; owner = consumer name
  (`hostname-uuid`). Dead worker's pending jobs are reclaimed every 15 s and run through
  the same pipeline; the DB idempotency check makes this safe.
- Backpressure leaves the job unacked (unchanged CP03 contract) and never counts toward
  dead-lettering. Processing *exceptions* are counted per message; at
  `QUEUE_MAX_DELIVERIES` (5) the job moves to `calls:dlq` and a durable `queue.dead_lettered`
  **AuditLog row in PostgreSQL** records contact, campaign, attempt number, reason,
  retry count, correlation id and timestamps. (No new table was added.)
- Worker loop backs off exponentially (max 30 s) on Redis/PostgreSQL errors; nothing is
  dialed while a dependency is down.
- Graceful shutdown (SIGTERM/SIGINT, compose `stop_grace_period: 45s`): stop reading, finish
  the in-flight job (commit + ack), close Redis, dispose the DB engine.

## 6. Admission control and re-checks

Before every dial (`_dial`): job idempotency → retry budget (`attempt_number ≤ max_retries+1`)
→ campaign active / contact status / suppression / calling window (existing
`DialEligibilityService`, never trusting enqueue-time results) → Dograh capacity →
Redis admission (CPS, concurrency reservation, backpressure, circuit breaker) → Dograh
configuration. Dograh in-flight concurrency (global and per campaign) is counted from
PostgreSQL attempts in `INITIATED/CONNECTED`; a race between workers can overshoot by at
most the number of workers. Admission/circuit keys use provider `dograh` when
`CALLING_ENGINE=dograh`. **Not implemented:** per-campaign budget/call-limit (not supported by the repo).

## 7. Recovery

Unchanged canonical policy (`RetryPolicy`: `max_retries=2`, spacing 30 s / 10 min, 3 total
attempts, rejected/invalid/opt-out never retried). CP09 only *feeds* it correct events:
definite dial failures, no-answer/busy, drops, and reconciled outcomes. Ambiguous calls
reach it only after reconciliation. Dial-time re-checks (above) are the "reload state →
suppression → campaign → retry count → window → capacity" gate, applied when the retry
actually fires rather than when it was scheduled.

## 8. Circuit breaker — `telephony/circuit_breaker.py`

`CLOSED →(≥`CIRCUIT_BREAKER_ERROR_THRESHOLD` failures within `CIRCUIT_BREAKER_WINDOW_SECONDS`)→
OPEN →(`CIRCUIT_BREAKER_OPEN_SECONDS`)→ HALF_OPEN →(probe ok)→ CLOSED`; a failed probe
reopens for a full cooldown. HALF_OPEN lets **one** probe through (`SET NX`). State is in
Redis so all workers agree. Only the Dograh boundary is wrapped (429 doesn't count).
Redis/PostgreSQL are protected by fail-safe behaviour rather than a breaker: with either
down the worker dials nothing and backs off. Tests: outage ⇒ trigger count == threshold.

## 9. Observability

- JSON logs (`core/logging_config.py`): `correlation_id`, `attempt_id`, `campaign_id`,
  `contact_id`, `provider`, `workflow_run_id`, `worker_id` where applicable. Phone numbers are
  masked in messages **and** extra fields (`+919876543210` → `+91******3210`); fields named
  like secrets are redacted; exception messages are never logged (type only); URLs from
  providers are never logged.
- Counters (Redis hash `metrics:counters`): calls_initiated/connected/failed/completed/partial,
  retry_count, webhook_count/duplicates/failures, dograh_errors, worker_failures, stale_jobs,
  dlq_entries, opt_out_count, circuit_opened. Gauges computed on read
  (`GET /api/v1/admin/dashboard/system/metrics`, admin JWT): active_calls, queue_depth,
  oldest_job_age_seconds, dlq_size, analysis_queue_depth.
- **Not done:** Prometheus exposition, latency histograms (latency is logged per call as
  `latency_ms`), `RETRY_RATE` (derive from counters), trace-id propagation beyond the
  existing job `trace_id` (used as `correlation_id`).
- `GET /health` & `/health/live`: liveness only. `GET /health/ready`: PostgreSQL + Redis,
  `503` if either fails, component ok/fail only.

## 10. Security

- Startup validation (`Settings` model validator) when `ENVIRONMENT=production`: fails fast,
  naming variables (never values), if JWT key, admin/operator passwords, telephony and Dograh
  webhook secrets, Dograh API key are empty/dev placeholders, DB/Redis point at localhost or
  default credentials, `LOG_LEVEL=debug`, or (engine=dograh) trigger uuid / workflow id /
  non-localhost base URL are missing or `DOGRAH_TRIGGER_MODE != production`.
- Rate limiting (Redis fixed window per client IP; fails **open** if Redis is down because
  the credential checks still apply): admin login 10/min, Dograh webhook 600/min. Internal
  worker operations are not rate limited.
- Audit: admin login success/failure (password never stored), campaign status
  change with the human actor, DLQ events, call state transitions. **Gap:** suppression added by
  the in-call path is audited by the existing flow; emergency-stop, retry-override and provider-config
  endpoints do not exist in this repo, so there is nothing to audit yet.
- RBAC reviewed: all `/api/v1/admin/*` routes use `require_admin` / `require_role("admin")`;
  tested that an operator receives 403 on campaign status change and unauthenticated gets 401/403.
- Container runs as a non-root user; DB/Redis ports are unpublished by `docker-compose.prod.yml`.

## 11. OUR-BACKEND configuration (existing names retained)

| Spec name | Variable here | Notes |
|---|---|---|
| APP_ENV | `ENVIRONMENT` | `production` enables validation |
| DATABASE_URL | `PRIMARY_DB_URL` | |
| REDIS_URL | `REDIS_URL` | enable AOF |
| DOGRAH_URL | `DOGRAH_API_BASE_URL` | |
| DOGRAH_API_KEY | `DOGRAH_API_KEY` | |
| DOGRAH_AGENT_ID | `DOGRAH_TRIGGER_UUID` | + **`DOGRAH_WORKFLOW_ID`** (new, needed for reconciliation) |
| DOGRAH_WEBHOOK_SECRET | `DOGRAH_WEBHOOK_SECRET` | |
| mode | `DOGRAH_TRIGGER_MODE=production` | |
| timeouts | `DOGRAH_CONNECT_TIMEOUT_SECONDS`, `DOGRAH_REQUEST_TIMEOUT_SECONDS` | |
| reconcile | `DOGRAH_RECONCILE_AFTER_SECONDS`, `DOGRAH_STALE_ATTEMPT_SECONDS` | |
| queue | `QUEUE_*`, `QUEUE_MAX_DELIVERIES`, `QUEUE_DLQ_STREAM_KEY` | |
| limits | `GLOBAL/CAMPAIGN/PROVIDER_{CPS,CONCURRENCY}_LIMIT` | |
| breaker | `CIRCUIT_BREAKER_ERROR_THRESHOLD/OPEN_SECONDS/WINDOW_SECONDS` | |
| rate limits | `LOGIN_RATE_LIMIT_PER_MINUTE`, `WEBHOOK_RATE_LIMIT_PER_MINUTE` | |

## 12. DOGRAH-SIDE configuration (cannot be done from this repo)

1. Publish the workflow; create an **API Trigger** node; copy its UUID → `DOGRAH_TRIGGER_UUID`;
   note the workflow's integer id → `DOGRAH_WORKFLOW_ID`; create an API key → `DOGRAH_API_KEY`.
2. Add a **Webhook** node on completion → `POST https://<backend>/api/v1/webhooks/dograh/call-completed`
   with credential `DOGRAH_WEBHOOK_SECRET` (Bearer or `X-API-Key`) and the payload template from
   CP08 notes (must include `call_attempt_id` from `initial_context`, `workflow_run_id`,
   `call_status`, `call_disposition`/`mapped_call_disposition`, `duration_seconds`, `transcript_url`).
3. Optionally set the node's `retry_config` so Dograh retries webhook delivery (our endpoint is
   idempotent; the reconciler is the safety net regardless).
4. Make the workflow emit a DNC-style disposition when a customer opts out.
5. Set Dograh's own org concurrency ≥ `GLOBAL_CONCURRENCY_LIMIT` (otherwise Dograh answers 429).
6. Alert on `authentication_error` (rotated/revoked key causes bounded failures, not retries forever).

## 13. Deployment, rollback, troubleshooting

Deploy: build images → set `backend/.env.production` from a secret manager →
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`. Only the API container
migrates (`RUN_MIGRATIONS=false` on workers). **No migration in CP09**, so rollback is code-only:
redeploy the previous image; no data migration to undo. Calls in flight survive: attempts are in
PostgreSQL and the previous build's webhook still works (it just lacks the extra guards).
Run ≥ 2 worker replicas; size workers ≈ peak dial rate × trigger latency (a worker is sequential).

| Symptom | Likely cause / action |
|---|---|
| Attempts stuck `INITIATED` with `provider=dograh` | Webhook not arriving; reconciler resolves after 120 s. Check `DOGRAH_WORKFLOW_ID`, webhook node URL/secret |
| Many `AMBIGUOUS_PROVIDER_STATE` events | Dograh slow/unavailable; breaker should open; check `dograh_errors`, Dograh health |
| Webhook 401 | Secret mismatch / auth type; 409 `workflow_run_id does not match` ⇒ wrong workflow/stale payload |
| Jobs piling in `calls:dlq` | Inspect `queue.dead_lettered` audit rows for reason |
| Worker exits at boot | `Invalid production configuration; fix these variables: …` |
| `/health/ready` 503 | PostgreSQL or Redis unreachable (component named in body) |

## 14. Capacity (baseline: 100,000 contacts/month, 35% connect, 3.5 min connected)

Derived: 35,000 connected calls × 3.5 min = **122,500 connected minutes/month**; ≤ 300,000
dial attempts/month at the 3-attempt ceiling (≈ 0.12/s averaged, a few/s at peak). *Assumption
(mine):* dialing happens in a ~8 h × 22-day window, giving ≈ 12 average / a few tens peak
simultaneous connected calls — set `GLOBAL_CONCURRENCY_LIMIT` and Dograh's limit accordingly.

Measured here (PostgreSQL 16 + Redis 7 on one sandbox host, fake Dograh): enqueue 100k jobs in
3.5 s; reading from a 100k-deep stream stays O(ms); **≈ 77 dials/s per worker** end to end. The
backend is not the bottleneck; real trigger latency is (sequential workers). Concurrency tests
(16-way duplicate webhook burst; 8 workers racing on one job duplicated 8×) prove exactly-once.
**Not load-tested:** Redis restart under load, PostgreSQL connection-pool exhaustion, real Dograh.

## 15. E2E validation procedure (**NOT VERIFIED in CP09** — no reachable Dograh instance)

With a real Dograh + a phone you control: (1) set production config; (2) enqueue one contact;
(3) confirm `PROVIDER_ACCEPTED` event and attempt `INITIATED` (not connected); (4) answer, talk,
hang up; (5) confirm webhook 200, attempt `INITIATED→CONNECTED→ENDED_NORMALLY`, transcript rows,
analysis row; (6) re-POST the same webhook ⇒ `already_processed`; (7) reject the call ⇒
`FAILED_TO_CONNECT(rejected)` with no retry; let one ring out ⇒ `no_answer` retry in 30 s;
(8) say "don't call me again" with the DNC disposition ⇒ suppression + no retry; (9) kill the webhook
URL during a call ⇒ reconciler completes it within ~2.5 min; (10) record the real `call_status`
strings seen and extend `dograh_webhook_service.py` if needed.
