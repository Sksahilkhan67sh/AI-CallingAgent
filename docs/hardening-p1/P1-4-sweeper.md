# P1-4 stuck-call sweeper: policy

`python -m app.sweeper` (compose service `sweeper`; `--once` for a single pass).

**Deviation from the task text, on purpose (rule 5).** The task said an attempt with no provider call
id is safe to requeue once. For Dograh it is not: Dograh has no idempotency key, and a worker can die
after the trigger request was accepted but before the run id was saved, leaving the same database state
as "never triggered". So the sweeper never dials and never enqueues a dial. Both stuck shapes end in
`NeedsReview`:

| Stuck shape | Attempt | Contact |
|---|---|---|
| no provider call id (died before trigger, or after trigger before the id was saved) | `FAILED_TO_CONNECT` + `DOGRAH_TRIGGER_AMBIGUOUS` marker | `NeedsReview` |
| provider call id exists, no completion webhook after the threshold | left `INITIATED` | `NeedsReview` |

A late completion webhook still wins in both shapes (the ambiguous marker keeps the existing adoption
path open; an `INITIATED` attempt finalizes normally) and replaces `NeedsReview` with the real outcome.
Every action writes a `SWEEPER_NEEDS_REVIEW` call event and a `sweeper.needs_review` audit event.
Rows are claimed with `FOR UPDATE SKIP LOCKED` in the acting transaction, so two sweepers never act on
the same row. While the kill switch or a provider halt blocks outbound, a pass does nothing.
Cost of the policy: a call that was never placed is not retried automatically. An operator reopens it.
