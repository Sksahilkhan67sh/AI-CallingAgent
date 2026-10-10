# Runbook — post-call analysis (CP14B)

All commands below exist today. There is no operator CLI or retry endpoint (DEFERRED); recovery is automatic via
the sweeper, and the SQL here is for inspection and deliberate, approved intervention.

## Inspect
```sql
SELECT status, count(*), min(created_at) AS oldest FROM call_analysis GROUP BY status;
SELECT id, status, error_code, attempt_count, next_attempt_at, lease_expires_at
  FROM call_analysis WHERE status IN ('pending','retry_wait','processing') ORDER BY created_at LIMIT 50;
```
Worker logs: `analysis_sweep` (finalized / registered / published / pending_count / oldest_pending_age_seconds),
`analysis_deferred`, `analysis_stale_claim_discarded`, `analysis_publish_failed_after_commit`.
Alert thresholds (suggested; delivery UNVERIFIED): oldest pending > 30 min, `publish_error=true` for > 5 sweeps,
FAILED count rising.

## Stuck work
* `processing` with an expired `lease_expires_at`: nothing to do — the next sweep republishes it. If the sweeper is not
  running, restart `python -m app.analysis_worker` (all state is durable).
* `retry_wait` you want sooner: `UPDATE call_analysis SET next_attempt_at = now() WHERE id = '<id>' AND status = 'retry_wait';`
  (the sweeper publishes it; attempts are not reset).
* After Redis loss: nothing manual; rows are republished within `ANALYSIS_REPUBLISH_AFTER_SECONDS`.

## Failure codes (`error_code`)
`provider_permanent_error` (bad key / unknown run / bad ids — fix `DOGRAH_API_KEY`, `DOGRAH_BASE_URL`, workflow id),
`provider_rate_limited`, `provider_timeout`, `provider_unavailable`, `invalid_output` (QA prompt drifted from the
`cp14b.v1` contract — see notes §3), `qa_result_not_produced` / `qa_unavailable` (QA sampled out, disabled, call too
short), `empty_conversation`, `no_conversation_session`, `not_eligible`, `invalid_call_relationship`,
`missing_provider_run_id`, `lease_expired_attempts_exhausted`, `budget_not_configured`, `budget_cap_reached`.

## Budget
```sql
SELECT day, reserved_cost FROM analysis_budget_day ORDER BY day DESC LIMIT 7;
SELECT error_code, count(*) FROM call_analysis WHERE error_code LIKE 'budget_%' GROUP BY 1;
```
`budget_not_configured` ⇒ set `ANALYSIS_DAILY_ESTIMATED_SPEND_CAP` and `ANALYSIS_ESTIMATED_COST_PER_ANALYSIS`
(together) and restart. These are estimates; Dograh's QA cost is incurred independently (notes §6).

## Reprocessing a terminal row (FAILED/SKIPPED)
Not automated and not audited — requires owner approval. Dograh cannot re-run QA via the documented API, so
reprocessing only re-reads the run; it helps only after fixing credentials/ids/prompt and after QA has produced
output. Never bulk-reset: each reprocess may be a billed read and re-enters the budget gate.

## Invalid model output
Read `error_code='invalid_output'` rows; compare the QA node's prompt with notes §3. The backend never repairs or
stores invalid output; it retries up to `ANALYSIS_MAX_ATTEMPTS`, then FAILED.

## Deploy / rollback
See notes §11. Never downgrade the schema while `retry_wait`/`skipped` rows exist; the migration refuses.
