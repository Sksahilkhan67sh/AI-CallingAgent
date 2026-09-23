# Checkpoint 06 — Post-Call Intelligence — implementation notes

## Architecture

    Terminal Call (orchestrator._end_conversation / recovery._terminalize)
        -> Analysis Admission (app/services/analysis/admission.py)
        -> Analysis Queue (Redis Stream "analysis:jobs", group "analysis-workers")
        -> Analysis Worker (app/analysis_worker.py, app/services/analysis/worker.py)
        -> Load durable conversation data (Postgres)
        -> Transcript Preparation
        -> LLM Analysis (mock provider; real provider is a config swap)
        -> Deterministic scoring
        -> Persist CallAnalysis
        -> Emit CallEvent + AuditLog
        -> ACK

Admission is a single function, `enqueue_call_analysis(db, call_attempt,
contact)`, called unconditionally from the two places a CallAttempt
already reaches a terminal outcome in this repository:
`ConversationOrchestrator._end_conversation()` and
`RecoveryManager._terminalize()`. It never invents a new terminal-state
concept — eligibility is derived entirely from the existing
`CallAttemptState`/`ContactStatus` enums.

## Eligibility (§3) — and a spec/code discrepancy this checkpoint had to reconcile

`docs/specs/AI_Calling_Agent_Core_Production_Logic.md` (§26-27) is
explicit: analysis runs for `Completed`/`CompletedPartial`, never for
`Closed` — and `Closed` covers *two* different real situations: a call
that never connected (retries exhausted), and a call that connected but
the customer opted out. Both are meant to produce **no analysis**.

The actual CP05 code doesn't fully separate these:
`RecoveryManager._terminalize()` sets `contact.status =
COMPLETED_PARTIAL` for **both** "dropped mid-call, no more retries" and
"never connected, no more retries" — it doesn't check which. Relying on
`contact.status` alone would incorrectly admit analysis for calls that
never connected and have no transcript.

This checkpoint does not touch that CP05 decision (explicitly out of
scope to redesign prior checkpoints). Instead, admission adds its own
defense-in-depth check on `call_attempt.state`:

- `state == FAILED_TO_CONNECT` -> never eligible, regardless of
  `contact.status`. A call that never connected has no conversation.
- `state in (ENDED_NORMALLY, DROPPED_MID_CALL)` AND `contact.status in
  (COMPLETED, COMPLETED_PARTIAL)` -> eligible.
- Everything else (non-terminal statuses, `CLOSED`) -> not eligible.

The opt-out case is handled correctly for a different reason:
`_end_conversation()` sets `contact.status = CLOSED` specifically for
`reason == "opt_out"`, and admission's status check already excludes
`CLOSED`.

`tests/test_call_analysis_recovery_integration.py` exercises this
directly: a never-connected `FAILED_TO_CONNECT` attempt, terminalized
through the *real* `RecoveryManager.handle_disconnect()`, ends up with
`contact.status == COMPLETED_PARTIAL` (the existing CP05 behavior) but
**no** `CallAnalysis` row is created.

## Domain model — one new table, no changes to existing ones

`call_analysis` (`app/models/call_analysis.py`,
`alembic/versions/d15c334bf7e7_...py`): one row per `CallAttempt`
(`UNIQUE(call_attempt_id)`), FKs to `call_attempt`, `contact`,
`campaign`, and nullable `conversation_session`. Five new Postgres
enums (`analysis_status`, `analysis_intent`, `interest_status`,
`analysis_sentiment`, `analysis_next_action`) in `app/models/enums.py`,
distinct from the existing per-turn `Intent`/`NextAction` enums CP04
already defines for the live conversation engine — different concepts,
same-looking names would have been confusing.

Migration verified: `upgrade -> downgrade -> upgrade` on both the dev
and test databases. `downgrade()` explicitly drops the five new
Postgres ENUM types after dropping the table (the same fix already
applied in the initial-schema migration — `op.drop_table()` alone
leaves orphaned Postgres types, breaking a subsequent `upgrade`).

## Idempotency (§6)

Postgres is authoritative, never Redis. Two separate atomic operations:

- **Admission time**: `CallAnalysisRepository.get_or_create_pending`
  does a `SELECT` then an `INSERT` inside a `SAVEPOINT`; a duplicate
  admission (two terminal-state paths, or a retry of the same one)
  loses the unique-constraint race and just returns the existing row
  without enqueuing a second job.
- **Worker time**: `CallAnalysisRepository.claim_for_processing` is a
  single conditional `UPDATE ... WHERE status IN (PENDING, FAILED) AND
  attempt_count < max_attempts RETURNING id`. A `COMPLETED` analysis,
  or one already claimed by a live worker, or one that exhausted its
  attempts, all fail to match and the worker treats that as a no-op
  rather than reprocessing.

Tested explicitly: duplicate admission (row + Redis job), duplicate
in-flight delivery replay (crash-before-ack simulation), and duplicate
enqueue never producing two analyses.

## Queue and retry/backoff (§7-8, §21-22)

A dedicated Redis Stream (`analysis:jobs` / group `analysis-workers`),
structurally identical to CP03's `RedisStreamQueue` but for
`AnalysisJob` — duplicated rather than generalized into a shared base,
since the two carry different payload types and a two-call-site generic
abstraction wasn't judged worth the indirection.

**A gap found in the existing CP03 reclaim mechanism.** `app/worker.py`
calls `queue.reclaim_stale(...)` (which does `XAUTOCLAIM`, transferring
ownership of a stale pending message to the calling consumer) but never
actually re-drives `process_one_job` on what it returns — it only logs
the count. `grep` across `tests/` confirms no test exercises this path;
it's inert. Since CP06's own retry-backoff design depends on a
reclaimed job actually being reprocessed, `app/services/analysis/worker.py`
exposes `process_claimed_job` as a public function (used both for a
freshly read job and for one handed back by `reclaim_stale`), and
`app/analysis_worker.py`'s loop explicitly calls it for every reclaimed
job. This is new CP06 code working correctly, not a fix applied to
CP03's `app/worker.py`, which is left untouched.

Retry model, two layers:

- **In-process** (`MAX_INPROCESS_LLM_RETRIES = 1`): mirrors
  `ConversationOrchestrator`'s own LLM-retry pattern — one immediate
  retry within the same delivery for a transient timeout/provider
  error.
- **Cross-delivery** (`analysis_max_attempts = 3`, config): a job that
  still fails is left **unacked** rather than acked-and-requeued. This
  gives a natural backoff window — `AnalysisQueue.reclaim_stale` only
  picks it up again after `analysis_reclaim_idle_ms` (default 60s) of
  idle time — reusing the crash-recovery mechanism as the backoff
  mechanism, rather than building a second delayed-queue (which CP05's
  `RecoveryScheduler` sorted-set pattern exists for, but is explicitly
  off-limits to reuse for call/retry-infrastructure reasons, and isn't
  needed here). Once `attempt_count` reaches the cap, the analysis is
  marked terminally `FAILED` and the message **is** acked — never
  retried again.

Malformed/invalid LLM output is never persisted as a completed
analysis — `AnalysisLLMValidationError` is treated the same as a
transient provider failure (retried up to the cap, then terminally
failed), and the empty-transcript short-circuit (`app/services/analysis/worker.py`)
produces an explicit deterministic result rather than calling an LLM
with nothing to analyze.

## LLM contract and deterministic scoring (§12, §14)

`AnalysisLLM.analyze(transcript_lines, brand_name) -> AnalysisResult`
(`app/services/analysis/llm/`). `AnalysisResult` carries `summary`,
`intent`, `interest_status`, `sentiment`, `next_action`, `feedback`,
`key_facts`/`objections`/`customer_needs`, `language`, and a
`ScoringSignals` struct — the LLM never returns a numeric lead score
directly. `app/services/analysis/scoring.py::compute_lead_score` is the
one place a 0-100 score is produced, from fixed, documented per-signal
weights (see the module docstring), clamped to `[0, 100]`, with
`opted_out` flooring the score at 0 regardless of other signals. Only a
`mock` provider (`FakeAnalysisLLM`, keyword-driven, deterministic) is
implemented — no real credentials exist in this environment;
`analysis_llm_provider` is a config value for swapping in a real one
later.

## Events, audit, observability

`ANALYSIS_QUEUED` / `ANALYSIS_COMPLETED` / `ANALYSIS_FAILED` /
`ANALYSIS_RETRY_SCHEDULED` `CallEvent` rows (reusing the existing
free-text `event_type` column, same as CP05's recovery events) plus a
matching `AuditLog` entry via the existing `record_audit_event` helper,
at every state transition. No transcript content is ever put in an
event/audit payload. No new metrics framework — logging follows the
existing `logger.info(..., extra={...})` convention used by
`app/worker.py`.

## API (§37)

Three read-only endpoints under `/api/v1` (`app/api/routes/call_analysis.py`):
`GET /call-attempts/{id}/analysis`, `GET /contacts/{id}/analysis`
(latest), `GET /campaigns/{id}/analysis` (paginated, same `Page[]`
convention as `contacts`/`campaigns`). No dashboard, filtering UI, or
CRM surface — explicitly CP07's scope.

## Cross-checkpoint isolation (§0, §34)

`tests/test_call_analysis_integration.py::test_analysis_failure_does_not_touch_call_or_campaign_state`
asserts directly: a failed analysis run touches no `CallAttempt`,
`Contact.status`, `Contact.attempt_count`, or `recovery:scheduled`
Redis state. `RecoveryManager` is never imported by anything under
`app/services/analysis/`.

## Known limitations / intentionally deferred

- Only a mock LLM provider exists; no real provider integration or API
  key handling.
- No tenant/authorization model exists anywhere in this repository yet
  (Checkpoint 02's own deferred note, `_ACTOR = "api-client"`), so
  there is nothing yet to scope the new endpoints against — they're
  world-readable within this app exactly like the existing `contacts`/
  `campaigns` endpoints are.
- No recording/media storage exists in this repository yet, so §26's
  "store references, not binaries" is moot for this checkpoint; the
  data model doesn't preclude adding recording references later.
- Reprocessing (§29) is not built — the `UNIQUE(call_attempt_id)`
  constraint intentionally keeps the model ready for an explicit
  future admin/reprocessing workflow (bump `analysis_version`, allow a
  second row per attempt) without requiring a migration to add it.
- Worker count is horizontal-by-process (run more `python -m
  app.analysis_worker` instances), identical to how `app/worker.py` is
  already scaled — no new orchestration was added for this checkpoint.

## Tests and verification

- 42 new tests across `tests/test_call_analysis_admission.py` (§32.A,
  eligibility + duplicate-admission idempotency),
  `tests/test_call_analysis_transcript.py` (§32.C),
  `tests/test_call_analysis_scoring.py` (§32.E),
  `tests/test_call_analysis_worker.py` (§32.B/D/G/H — LLM
  success/timeout/provider-error/malformed-output, retry-then-terminal-
  fail, already-completed no-op, reclaim-and-redeliver, missing-row
  data-error handling),
  `tests/test_call_analysis_api.py` (§37),
  `tests/test_call_analysis_integration.py` (§32.J full lifecycle +
  §0/§34 cross-checkpoint isolation),
  `tests/test_call_analysis_orchestrator_integration.py` and
  `tests/test_call_analysis_recovery_integration.py` (verifying
  admission is actually reached from the two real terminal-transition
  call sites, including the never-connected-vs-mid-call-disconnect
  discrepancy above).
- Full suite: `228 passed` (186 pre-existing + 42 new), all against a
  real PostgreSQL 16 + Redis 7 instance, no SQLite/fakeredis.
- `ruff check .`: all checks passed.
- `mypy app/`: no issues found in 110 source files.
- Migration verified with a real `upgrade -> downgrade -> upgrade`
  cycle on both `ai_calling_agent` and `ai_calling_agent_test`.
- No secrets/tokens/debug prints introduced (see PR description's
  validation-gate section).

Not claimed: "production-ready" for a real LLM provider, a real
tenant/authz model, or real recording storage — none of those exist
anywhere in this repository yet, and this checkpoint's own scope
(§0, §39) is the intelligence pipeline, not general production
hardening.
