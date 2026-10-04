# Checkpoint 10 — Provider / Voice production readiness — implementation notes

Every statement below carries one label:

- **VERIFIED** — proven by an automated test against real Postgres/Redis in this
  checkpoint, or by reading the actual code.
- **UNVERIFIED** — believed correct but not proven; almost always because no real
  Dograh instance was available.
- **BLOCKED** — could not be done in this environment.
- **DOCUMENTED LIMITATION** — a known gap that is accepted for now.
- **DEFERRED** — intentionally left to a later checkpoint.

Final test/lint/mypy/alembic numbers are in the PR description (they are produced
by the final gate run, not copied here).

## 1. Objective

Make the backend ⇄ Dograh execution path safe for production: classified provider
errors, no blind retriggering after an ambiguous result, deterministic status
normalization, validated correlation, fail-closed configuration. No second retry
engine, no business logic moved into Dograh, no LLM control of infrastructure.

## 2. Scope

In: `dograh_client`, `dograh_webhook_service` + route + schema, the Dograh branch
of `dialer_worker`, settings validation, `/ready`, log hygiene.
Out (untouched): authentication redesign, multi-tenancy, recording storage,
post-call intelligence, observability platform, autoscaling, cost, sharding.
**No database migration** was needed or added — **VERIFIED** (`alembic check`:
"No new upgrade operations detected"; single head `d15c334bf7e7`).

## 3. Existing provider architecture (audit, from current code)

- **VERIFIED, production-capable:** Dograh trigger via `DograhClient` with
  `call_attempt_id` in `initial_context`; durable attempt claim *before* the
  non-idempotent trigger; CP09 ambiguous-trigger reconciliation
  (`dograh_reconciliation`); webhook idempotency via `ProcessedEvent`; circuit
  breaker + `AdmissionController` in front of the trigger; failures routed to the
  single `RecoveryManager`.
- **VERIFIED, mocked:** the `native` calling engine only has mock providers.
- **Not in this repo:** STT, LLM, TTS, voice, language, barge-in, system prompt,
  telephony and from-number live in the *Dograh workflow*. The trigger API takes
  only `phone_number` and `initial_context`. **DOCUMENTED LIMITATION** — this
  backend can neither set nor verify them.

## 4. Dograh integration

`initiated` means *accepted for dialing*. **VERIFIED:** a successful trigger leaves
the attempt `INITIATED` and the contact `DIALING`, records
`provider="dograh"` and the run id, and never sets `CONNECTED`
(`test_accepted_trigger_is_initiated_not_connected_and_records_the_run`). Only the
completion webhook decides the outcome.

## 5. Provider contract

`DograhErrorCategory` (9 values) + `DograhConfigurationError` (raised before any
request exists) cover the ten required classes. **VERIFIED** (`test_cp10_provider_contract.py`):

| Situation | Category | Treated as |
|---|---|---|
| missing key/UUID | `DograhConfigurationError` | definite (no request sent) |
| 401 / 403 | `authentication_error` | definite |
| 400 / 422 | `validation_error` | definite |
| 404 | `provider_rejected` | definite |
| 429 | `rate_limited` | definite |
| 503, 501, other 5xx | `provider_unavailable` | definite |
| 500 / 502 / 504 | `ambiguous_request` | **ambiguous** (changed in CP10) |
| connect timeout / connect error / pool timeout | `connection_error` | definite (never reached Dograh) |
| read/write timeout | `ambiguous_request` / `timeout` | **ambiguous** |
| ReadError / WriteError / RemoteProtocolError | `ambiguous_request` | **ambiguous** (new in CP10) |
| 2xx with non-JSON / non-object / missing, non-int, ≤0 or bool `workflow_run_id` | `ambiguous_request` | **ambiguous** (new; was an uncaught `KeyError`) |
| anything else | `unknown_provider_error` | definite |

Provider error text is capped at 200 chars; it never contains the key or trigger
UUID — **VERIFIED**.

**UNVERIFIED:** that Dograh really returns 500/502/504 only when a run may exist
and 503 only when it does not. The split is a conservative judgment (ambiguous =
reconcile, never blind retry); a wrong guess costs a delayed retry, not a duplicate call.

## 6. Status normalization

`_normalize(call_status, call_disposition)` in `dograh_webhook_service.py`.
Dograh publishes **no enum** for either field — **UNVERIFIED** against a live
instance; the keyword/whole-token vocabulary is inherited from CP08/CP09 and
unchanged. No status names were invented.

Rules — **VERIFIED** (`test_cp10_webhook_lifecycle.py`):

1. `call_status` is the lifecycle authority; case/whitespace/`-`/space variants are tolerated.
2. Empty, `None` or unrecognized status ⇒ `FAILED_TO_CONNECT` / `PROVIDER_ERROR`,
   `recognized=False`, **no** conversation session, **no** analysis. Unknown never
   becomes success.
3. An unrecognized disposition is ignored (business codes are free text).
4. A recognized *failure* disposition may supply the outcome when the status is unknown.
5. A normal-completion disposition never rescues an unknown status.
6. Status and disposition that disagree on "ended normally?" are a **conflict**:
   not trusted as success, handled as provider error, audited with `basis="conflict"`.

STATE / OUTCOME / REASON / ANALYSIS stay separate fields; nothing was collapsed.
**DOCUMENTED LIMITATION:** matching is keyword-based, so a free-text disposition such
as `busy_call_back_later` is read as "busy" and, next to `user_hangup`, becomes a
conflict (safe direction, but a false negative on a real success).
**DEFERRED:** do-not-call / opt-out normalization — Dograh publishes no such code and
none was invented.

## 7. Voice configuration

- **VERIFIED:** all provider settings come from environment/settings; no secrets,
  numbers or workflow UUIDs in source (see §16).
- **VERIFIED (fail-closed, new):** `ENVIRONMENT`, `CALLING_ENGINE`, `DOGRAH_TRIGGER_MODE`
  are closed sets — a typo is a startup error (before: a typo silently skipped the
  production checks, ran the native engine, or hit Dograh's `/test/` endpoint).
  Timeouts must be > 0. Dograh base URL must be http(s) with a host.
- **VERIFIED:** `production` refuses `CALLING_ENGINE=native`, requires Dograh key +
  UUID + webhook secret, `DOGRAH_TRIGGER_MODE=production`, and an **https, non-local**
  Dograh URL. `staging` requires key + UUID. `development`/`test` boot without them.
- **VERIFIED:** `/ready` reports "Dograh configuration" degraded (503) when key/UUID
  are missing, with no network call and no secret in the response.
- **DOCUMENTED LIMITATION:** the backend cannot check the Dograh-side workflow's
  STT/LLM/TTS/voice settings. **UNVERIFIED** whether recording is enabled there.

## 8. Correlation

New `_validate_correlation` runs before any mutation and before the terminal-attempt
no-op. A webhook is rejected (HTTP 409, audit `dograh.webhook_correlation_rejected`,
nothing mutated) when: **VERIFIED**

- the attempt belongs to a non-Dograh provider;
- the payload's optional `contact_id` / `campaign_id` echo does not match;
- `workflow_run_id` differs from the attempt's recorded run (`run_mismatch`) — even if
  the attempt is already terminal;
- the run is already owned by a different attempt (would hit
  `uq_call_attempt_provider_call_id`).

Echo fields are optional so existing CP08 workflow templates keep working.
**DOCUMENTED LIMITATION:** a template that does not echo `contact_id`/`campaign_id`
is protected by `call_attempt_id` + run id only. Dograh's webhook body can only echo
what the workflow template is configured to send — **UNVERIFIED** against a live workflow.

## 9. Webhook lifecycle

Order is now: rate limit → **authentication** → schema → attempt lookup →
correlation → replay/idempotency → normalization → transition → durable update →
audit/analysis. **VERIFIED:**

- unauthenticated caller gets 401 and no schema feedback (auth is a route dependency
  that runs before body validation); non-ASCII credential is a clean 401, not a 500;
  comparison stays `secrets.compare_digest`, now on bytes;
- duplicate webhook ⇒ `already_processed`, one conversation session;
- malformed authenticated payload ⇒ 422, attempt untouched;
- completion webhook arriving before the dialer persisted the run id completes the
  attempt **and** records the run id;
- webhook after an ambiguous trigger adopts the run without a second call (CP09 rule intact);
- the CP09 reconciliation, terminal-reopen and idempotency tests all pass unmodified
  in behaviour (two older tests had fixtures updated, see §11).

## 10. Failure handling

Through the real `process_one_job` with a deterministic fake client — **VERIFIED**
(`test_cp10_dialer_failures.py`). For every definite and ambiguous category: attempt
→ `FAILED_TO_CONNECT`, one call event with the category, job acked only after the
durable commit, circuit breaker counted, and **the trigger is never called a second
time** when the same job is redelivered.

- Unexpected exception ⇒ job left pending (not acked), durable claim survives the
  rollback, redelivery does not retrigger.
- **Fixed in CP10:** a `DograhConfigurationError` used to leave the contact stranded
  in `DIALING`; it now records a `DOGRAH_CONFIGURATION_ERROR` event and goes through
  RecoveryManager. It does not touch the circuit breaker.
- Open circuit ⇒ trigger not called, job not acked, no attempt row created.

### 429 / rate limiting — reviewed

Current behavior (**VERIFIED**): a 429 is a *definite* failure (no call was placed),
recorded as `PROVIDER_ERROR` never-connected, handed to RecoveryManager's existing
bounded backoff, and counted against the Dograh circuit breaker. After the breaker
threshold (default 5 errors) the breaker opens (default 30 s) and admission stops
all further triggers; refused jobs stay queued, unacked
(`test_rate_limit_responses_cannot_become_a_retry_storm`: with 8 queued jobs, exactly
5 trigger calls are made).

Decision: **intentional for CP10, not changed.** It keeps one retry engine and cannot
cause a storm or a duplicate call. **DOCUMENTED LIMITATION:** (a) a 429 consumes one
of the contact's bounded retry attempts; (b) `Retry-After` is not read; (c) a 429
counts toward the same breaker as 5xx errors. **DEFERRED:** `Retry-After`-aware
admission and a non-attempt-consuming retry for rate limits. No Dograh-specific CPS
limit was invented — **UNVERIFIED** what Dograh actually enforces.

## 11. Tests

New (120): `test_cp10_provider_contract.py` (30), `test_cp10_config.py` (23),
`test_cp10_webhook_lifecycle.py` (52), `test_cp10_dialer_failures.py` (15, which
includes the 429 storm test). Existing files changed:
`test_dograh_webhook_hardening.py`, `test_dograh_classification.py` — fixtures only:
those tests posted webhooks for a run id different from the one stored on the
attempt, which the new correlation check correctly rejects; they now post the
matching run id (`99` / `77`) and one event-id assertion changed from
`dograh:2001` to `dograh:99`. No assertion about behavior was weakened.

All test payloads are hand-written fixtures, **not** captured from Dograh.

## 12. Performance (SIMULATED — not Dograh, not telephony)

Harness: a local `ThreadingHTTPServer` fake in the **same Python process** as the
client, on a **1-vCPU** sandbox, so the numbers measure the harness's GIL-bound
ceiling, not the backend or Dograh. 300 requests per row, `DograhClient.trigger_call`:

| fake latency | threads | p50 ms | p95 ms | max ms | errors | req/s |
|---|---|---|---|---|---|---|
| 0 | 1 | 26.0 | 45.9 | 72.6 | 0 | 35 |
| 0 | 20 | 571.8 | 870.0 | 1036.2 | 0 | 33 |
| 50 ms | 20 | 614.1 | 751.3 | 824.8 | 0 | 34 |
| 200 ms | 50 | 1407.8 | 1888.9 | 2312.1 | 0 | 31 |
| 503 responses | 10 | 297.5 | 385.0 | 432.9 | 100/100 `provider_unavailable` | 33 |

Throughput is flat at ~33 req/s regardless of threads — the signature of a
single-core, shared-process ceiling. **Nothing here is a capacity claim.** An earlier
run with the default listen backlog showed spurious connection errors (2 and 37
failures); that was the fake server's backlog of 5, fixed in the harness
(`request_queue_size=256`) and unrelated to the client.
**BLOCKED/UNVERIFIED:** real Dograh trigger latency, webhook latency, telephony
capacity, and anything about 100K real calls. Benchmark script is not committed.

## 13. Live Dograh E2E

**BLOCKED / UNVERIFIED BY ENVIRONMENT.** No Dograh instance, API key, trigger UUID
or authorized test phone number was available. None of the 20 live checks in the
CP10 brief (answered call, STT/LLM/TTS turns, completion webhook from a real run,
no-answer, busy, hang-up, provider failure) was executed. No live result, webhook
payload or status value is claimed anywhere in this checkpoint.

## 14. Known limitations (DOCUMENTED LIMITATION)

- Status vocabulary is a heuristic, unverified against live Dograh (§6).
- 5xx ambiguous/definite split is a judgment (§5).
- 429 handling per §10.
- Voice/STT/LLM/TTS settings are Dograh-side and unchecked from here (§7).
- Correlation echo fields are optional (§8).
- SSRF guard cannot stop DNS rebinding between the address check and the fetch
  (§15); transcript fetch is best-effort and never fails webhook processing.
- A webhook with `workflow_run_id` absent is validated by `call_attempt_id` only.

## 15. Security findings

- **Fixed, VERIFIED:** `transcript_url` came from the webhook body and was fetched
  with no restriction — SSRF to loopback / cloud metadata / internal hosts, and
  redirects were followed. Now: loopback, link-local, unspecified, multicast and
  reserved targets are never fetched; private-network targets only for the Dograh
  API host or `DOGRAH_TRANSCRIPT_EXTRA_HOSTS`; redirects are not followed.
- **Fixed, VERIFIED:** webhook auth ran after body validation; non-ASCII credential
  caused a 500 (`TypeError` in `compare_digest`).
- **Fixed:** `httpx` INFO logging would print the Dograh trigger URL (contains the
  trigger UUID) in API and worker logs; set to WARNING.
- **Fixed, VERIFIED:** provider error detail bounded to 200 chars.
- **Secret scan:** no tokens/keys in the diff; test fixtures use obvious placeholders
  (`dg_not_a_real_key`, `s-dograh`). `detect-secrets` flags test placeholders and
  pre-existing `dev-only-insecure-…` defaults in `config.py` (not added by this diff).
- **PII/log scan:** the new log lines carry only attempt/contact/campaign IDs, a
  category, or a problem code — no phone numbers, transcripts, keys or headers.
  Fixtures use `555-…` / `+1555010…` numbers only. The audit metadata for processed
  webhooks now includes `call_disposition` (a short workflow-defined code, ≤ the
  schema's status length cap).
- **Residual:** DNS rebinding on transcript fetch (**DOCUMENTED LIMITATION**).
  Webhook secret is a single shared secret (unchanged) — **DEFERRED** to the auth checkpoint.
- **Operational:** a GitHub token was shared in plain text for this work; rotate it.

## 16. Deferred

CP13 observability dashboards/metrics for provider events; Dograh-side workflow
validation (needs live access); `Retry-After`-aware admission; opt-out/DNC provider
codes; webhook secret rotation / per-tenant secrets; live Dograh E2E and any real
capacity numbers; DNS-pinned transcript fetch.

## 17. Final acceptance criteria

Mapped to the CP10 checklist. Test/lint/mypy/alembic counts: see PR.

- [x] Provider contract deterministic — **VERIFIED**
- [x] Trigger path correct; acceptance ≠ connection — **VERIFIED**
- [x] Provider errors categorized — **VERIFIED**
- [x] Statuses normalized safely; unknown never success — **VERIFIED** (vocabulary **UNVERIFIED** live)
- [x] Voice/provider configuration environment-driven and validated — **VERIFIED** (Dograh-side voice settings **BLOCKED**)
- [x] Production cannot silently fall back to the mock engine — **VERIFIED**
- [x] Correlation validated; wrong attempt/run/campaign rejected — **VERIFIED**
- [x] Webhook lifecycle idempotent; CP09 protections intact — **VERIFIED**
- [x] Duplicate calls prevented under every failure category — **VERIFIED**
- [x] Admission/rate/concurrency interaction safe (429 bounded by breaker) — **VERIFIED**
- [x] No secrets committed; PII-safe logging — **VERIFIED** (scans in §15)
- [ ] **Live Dograh E2E — BLOCKED/UNVERIFIED**
