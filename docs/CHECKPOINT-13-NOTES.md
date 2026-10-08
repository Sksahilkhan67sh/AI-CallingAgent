# Checkpoint 13 — Dograh critical fixes

Status: **NOT PRODUCTION READY — LIVE DOGRAH E2E: BLOCKED** (no Dograh credentials were available).
Everything below is verified against a deterministic fake and Dograh's *source code*, never a live instance.

## 1. Scope
C6 status classification, opt-out suppression, H1 provider-error budget, 429 + Retry-After,
bounded reconciliation, tests, docs. Out of scope and untouched: H3/H4/H5, CP14, frontend, Dograh
Campaigns, queue/Redis architecture, schema (no migration was needed).

## 2. Current Dograh architecture
Worker -> admission -> `DograhClient.trigger_call` (`POST /api/v1/public/agent/{uuid}`, `X-API-Key`)
-> `initiated` + `workflow_run_id` -> completion webhook (shared-secret, fail-closed) ->
`process_dograh_webhook` -> `RecoveryManager`. RecoveryManager remains the only retry owner.

## 3. Existing behavior (before CP13) and 4. Root causes
Audited by running every real Dograh `call_status` through the old keyword heuristic:
* `end_call` (agent ends a normal call) -> failure -> **retried** (duplicate call to a finished customer).
* `no-answer` (hyphenated) -> provider error. `voicemail_detected`, `call_transferred`,
  `call_duration_exceeded`, `user_idle_max_duration_exceeded` -> provider error.
* `ringing` / `in-progress` / `initiated` / `answered` -> retried while the call may be live.
* 401/403/404/400/402/409/422 -> all retried as provider errors. 408 -> plain validation error.
* 429 -> a generic provider error; Retry-After never read.
* The Dograh webhook path had no opt-out detection (only the native orchestrator did).
* A failed reconciliation lookup deferred the retry with no upper bound.

## 5. C6 classification design
`app/services/telephony/dograh_outcome.py` is the single decision point (trigger errors + webhook
outcomes + opt-out). It only reports; RecoveryManager decides retry vs terminal from `reason_key`.
Vocabulary provenance: **source-verified, not live-verified** — `TelephonyCallStatus`
(dograh-hq/dograh `api/enums.py`) and `EndTaskReason` (dograh-hq/pipecat `utils/enums.py`).
A test pins the vocabulary so a new Dograh value fails loudly.

| Dograh `call_status` | Internal | Retry | Suppress | Basis |
|---|---|---|---|---|
| completed, user_hangup, end_call, call_transferred, transfer_call, call_duration_exceeded, user_idle_max_duration_exceeded | ENDED_NORMALLY | no | no | a conversation occurred (judgment for the last three) |
| busy | never-connected / busy | per policy | no | verified value |
| no-answer | never-connected / no_answer | per policy | no | verified value |
| voicemail_detected | never-connected / no_answer | per policy | no | **judgment**: reached a machine |
| failed, canceled, error | never-connected / provider_error | budgeted | no | `error` is also stamped on runs Dograh rejected at start |
| unexpected_error, pipeline_error, system_cancelled | DROPPED_MID_CALL / technical_issue | per policy | no | `system_cancelled` is a judgment |
| initiated, ringing, in-progress, answered | none (ignored, event **not** claimed) | no | no | call may be live; real completion still processes |
| anything else / conflicting status+disposition | never-connected, `unrecognized_status` | **no (terminal)** | no | never guess; the call may have happened |

Dropped on purpose (no evidence Dograh emits them): the old `rejected`, `invalid_number`, `network_timeout`,
`customer_disconnect`, `agent-finished` keywords.

## 6. Provider HTTP status mapping (trigger)
Verified against Dograh `api/routes/public_agent.py`.

| Status | Meaning in Dograh | Policy | Retry |
|---|---|---|---|
| 2xx + run id | accepted for dialing | non-terminal | n/a |
| 2xx malformed / no run id | accepted? unknown | ambiguous -> reconcile | not blindly |
| 400 | telephony unconfigured **or** initiation failed (free text) | permanent | no |
| 401 / 403 | bad key / wrong org | permanent | no |
| 402 | quota exhausted | permanent | no |
| 404 | trigger missing/inactive | permanent | no |
| 408 | gateway timeout | ambiguous -> reconcile | not blindly |
| 409, 422 | no execution owner / bad request | permanent | no |
| 429 | concurrent-call limit (no Retry-After sent) | rate_limited | yes, bounded delay |
| 500, 502, 504, read timeout | response may be lost | ambiguous -> reconcile | after reconcile |
| 501, 503, connect failure | definitely no call | transient | yes, budgeted |

## 7. H1 provider-error budget
One bounded attempt budget (`RetryPolicy.max_retries`) plus a per-contact provider-error budget
(`DOGRAH_PROVIDER_ERROR_MAX_RETRIES`, default 2), counted from durable `call_attempt` rows
(survives restart, not double-spent by duplicate events). The lower bound wins. Customer outcomes
(no-answer, busy) do not spend it. 429 consumes the normal attempt budget only.

## 8/9. 429 and Retry-After
429 is retried via RecoveryManager under the campaign's `provider_error` rule. Delay =
`max(policy spacing, Retry-After)`, never shorter, ceiling `DOGRAH_RETRY_AFTER_MAX_SECONDS`
(3600), clamped in both the client and RecoveryManager. Seconds and HTTP-date are parsed; absent,
malformed, negative, past, oversized -> fallback to policy spacing. The existing circuit breaker still
counts 429s; no second rate limiter was added. Retry-After cannot bypass kill switch, pause,
suppression, calling window or admission (the dispatcher/worker re-check on fire).

## 10/11. Opt-out and suppression
Signal: `call_disposition` or `mapped_call_disposition` exactly equal (case/`-`/space-insensitive) to a
code in `DOGRAH_OPT_OUT_DISPOSITIONS` (default `["do_not_call"]`; `[]` disables). Dograh dispositions are
per-workflow and opt-in, so **this default is an assumption until live-verified**. No substring
matching, no transcript text, nothing from an LLM. Persisted with `INSERT … ON CONFLICT DO NOTHING
RETURNING` on `suppression.contact_id` in the same transaction as the attempt; only the inserter
audits (`dograh.opt_out_suppressed`); contact -> Closed (never relabelled Completed). Reuses
`SuppressionSource.AGENT_IN_CALL` (no enum migration). Queued/retry work is neutralized by the existing
durable worker/dispatcher eligibility checks; Redis is never the authority.

## 12. Reconciliation
Existing CP09/CP10 design kept (ambiguous trigger -> run lookup -> adopt / backoff -> second lookup
gate before any redial). CP13 adds: 408 as ambiguous; a durable bound on lookup-failure deferrals
(`DOGRAH_RECONCILE_MAX_DEFERRALS`, default 5, counted as audit rows). On expiry the retry is
**abandoned (no dial)**: preferring a missed retry over ringing a customer twice. Event names:
`reconciliation_started`, `reconciliation_expired` (+ existing `reconciliation_*`).

## 13-16. Correlation, idempotency, transitions, leases
`call_attempt_id` is sent in `initial_context`; `workflow_run_id` is stored as `provider_call_id` and
a mismatching run id is refused. Idempotency is unchanged (ProcessedEvent + unique claim). A new
event aimed at an already-terminal attempt is refused and audited (`dograh.invalid_provider_transition`).
CP12-A lease code is unmodified; tests assert zero leaked leases after every Dograh failure type.

## 17. Tests
`tests/test_cp13_dograh_critical.py`: 105 tests, real PostgreSQL + Redis. Existing tests changed
(intentional policy changes, not weakened): `test_dograh_classification.py`, `test_cp10_webhook_lifecycle.py`
(pure classifier tests rewritten to the verified vocabulary), plus one-line updates in
`test_cp10_dialer_failures.py`, `test_dograh_webhook.py`, `test_reconciliation_retry_gate.py`.
Full suite 1137 passed (twice; baseline 1048). CP10 124, CP08/09 group 129, CP11 399, CP12-A 39, CP12-B 27, CP12-C 39.
Ruff check, mypy, `alembic check`: pass.

## 18. Mutation results
Injected 30, caught 30, survived 0.

| # | Mutation | Result |
|---|---|---|
| 1 | initiated -> Completed (dialer) | CAUGHT |
| 2 | initiated -> Completed (vocabulary) | CAUGHT |
| 3 | remove 429 handling | CAUGHT |
| 4 | ignore Retry-After | CAUGHT |
| 5 | Retry-After in the past not rejected | CAUGHT |
| 6 | Retry-After unbounded (no ceiling) | CAUGHT |
| 7 | Retry-After seconds unbounded | CAUGHT |
| 8 | provider errors retry forever (budget removed) | CAUGHT |
| 9 | provider errors never retry | CAUGHT |
| 10 | opt-out does not create suppression | CAUGHT |
| 11 | duplicate webhook: idempotency guards removed | CAUGHT |
| 12 | duplicate webhook: pre-check removed too | CAUGHT |
| 13 | reconciliation gate removed (immediate retry) | CAUGHT |
| 14 | reconciliation never resolves (unbounded deferral) | CAUGHT |
| 15 | workflow_run_id ignored | CAUGHT |
| 16 | lease release removed | CAUGHT |
| 17 | invalid terminal transition accepted | CAUGHT |
| 18 | 401/403 treated as retryable | CAUGHT |
| 19 | 500 treated as permanent | CAUGHT |
| 20 | 429 treated as generic success | CAUGHT |
| 21 | duplicate suppression allowed (no ON CONFLICT) | CAUGHT |
| 22 | RecoveryManager bypassed | CAUGHT |
| 23 | opt-out audit not guarded by RETURNING (the bug found in testing) | CAUGHT |
| 24 | 408 treated as definite failure (no reconcile) | CAUGHT |
| 25 | opt-out substring match instead of exact | CAUGHT |
| 26 | provider budget counts customer outcomes too | CAUGHT |
| 27 | end_call treated as failure again | CAUGHT |
| 28 | unrecognized status becomes a retryable redial | CAUGHT |
| 29 | non-final status acted on | CAUGHT |
| 30 | permanent config error still retried (alias to provider_error rule) | CAUGHT |

## 19. Live Dograh E2E
**BLOCKED.** Missing: `DOGRAH_API_KEY`, trigger UUID/base URL for a real instance, a reachable webhook URL, and a
test phone number. Not verified live: `initiated` + run id, webhook arrival, real `call_status`/`call_disposition`
values, the `do_not_call` code, 429 body/headers, timeout reconciliation. Nothing was faked as a substitute.

## 20. Performance
Webhook handling p50 7.6 ms / p99 12.8 ms (300 deliveries); with 400k audit rows p50 9.1 ms / p99 15.2 ms.
Duplicate delivery ~0.6 ms. The reconciliation counter uses `ix_audit_log_entity` (index scan, ~0.2 ms).
No new O(N) work; no polling loops added.

## 21. Known limitations
* Live behavior unverified (above). Disposition code and the voicemail/system_cancelled mappings are judgments.
* A permanent config error (bad key) terminalizes each contact it touches, at circuit-breaker pace. Alert on
  `dograh.provider_configuration_error`. Holding the job instead would be safer but is a larger design change.
* 400 is treated as permanent although Dograh also uses it for "initiation failed" (free text).
* An attempt stuck at INITIATED because no final webhook arrives is H3 (sweeper), out of scope.
* Unknown statuses end the contact instead of retrying (behavior change; four existing tests updated).

## 22. Rollback
Code-only change, no migration: revert the PR. New env vars are optional with safe defaults; leaving them set
after a revert is harmless.
