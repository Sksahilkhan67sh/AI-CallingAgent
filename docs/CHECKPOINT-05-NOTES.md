# Checkpoint 05 — implementation notes / deviations

## No new database tables or migration

Inspected the existing schema first, per this checkpoint's own Step 3/23.
Everything CP05 needs already exists: `RetryPolicy` already has
`max_retries=2`, `retry_spacing_seconds=[30, 600]`, and per-reason
`never_connected_rules`/`mid_call_rules` (Checkpoint 01A); `CallEvent`
already takes an arbitrary `event_type` string, so the new recovery
event names (`RECOVERY_SCHEDULED`, `RETRY_ATTEMPT_CREATED`, etc.) need
no schema change; `AuditLog`, `Suppression`, `ProcessedEvent`, and
`WorkingMemorySnapshot` are all already generic enough to reuse as-is.
No Alembic migration in this checkpoint.

## Delayed scheduling: a Redis sorted set, not a new queue

The checkpoint explicitly rules out `asyncio.sleep()`/timers/in-memory
lists, and says to reuse the existing Redis Streams/durable queue
architecture — but a Stream has no concept of "deliver this at time T,"
only "deliver this now, to whoever's listening." A Redis sorted set
(`recovery:scheduled`, score = due-at unix timestamp) is the standard
durable-delay primitive on top of the same Redis instance already in
use, and it composes with the existing stream rather than replacing it:
once a scheduled job's time arrives, it is pushed onto the *same*
`calls:outbound` stream the CP03 dialer already consumes — so the
actual retry dial goes through the exact same admission control,
provider reconciliation, and CallAttempt-creation logic as every other
call, not a second dialer. `ZREM`'s return value (1 if this caller
actually removed the member, 0 if another worker already claimed it)
is the atomic claim — the same "whoever wins the DB/Redis race owns
the job" pattern used throughout CP03.

## `DialJob` gains two optional fields

`recovery_type: str | None` and `previous_attempt_id: str | None` —
present only on jobs produced by the recovery dispatcher, `None` for a
normal first-attempt dial. This is exactly the payload shape the
checkpoint's own Step 14 example shows (`recovery_type`, `attempt_id`
referring to the *previous* attempt). Needed so the dialer worker knows
to call `start_conversation(..., is_reconnect=True,
previous_attempt_id=...)` instead of starting a conversation from
scratch.

## Memory reconnect loading fixed to use the previous attempt's ID

Checkpoint 04's `ConversationOrchestrator.start()` loaded prior memory
using `self.call_attempt.id` — the attempt being *created now*. That
only made sense for a same-attempt reconnect (a brief same-call
hiccup), which CP04 stubbed out but never actually wired up. CP05's
retries always create a *new* `CallAttempt` row (Step 12), so the
memory that needs restoring was checkpointed under the *previous*
attempt's ID. `start()` and `start_conversation()` now take an explicit
`previous_attempt_id` and load from that when present, falling back to
the current attempt's ID otherwise (preserving CP04's original
same-attempt-reconnect behavior for whatever future case might still
want it).

## `ConversationOrchestrator.handle_disconnect(reason)` — new

CP04 only ever reached a *graceful* end (`_end_conversation`): opt-out,
goal met, max turns, unrecoverable AI-layer failure. It had no concept
of the call itself dropping mid-conversation — `CallAttemptState.DROPPED_MID_CALL`
existed in the enum since Checkpoint 01 but nothing ever set it. This
checkpoint adds the missing path: `handle_disconnect` sets
`DroppedMidCall` + the given `MidCallDisconnectReason`, checkpoints
memory one last time, ends the session, sets
`Contact.status = Disconnected` (the Call-State-Machine's own next
step from an active call — not `Closed`, which is reserved for
suppression/opt-out, and not `Completed*`, which implies the call
actually finished), and hands off to `RecoveryManager` for the
retry/terminal decision. It is a sibling to `_end_conversation`, not a
replacement — opt-out and other graceful ends still go through
`_end_conversation` exactly as before and never touch `RecoveryManager`
at all (Step 8: opt-out has the highest priority and schedules nothing).

## Never-connected failures also go through `RecoveryManager`

Checkpoint 03's dialer already set `CallAttemptState.FailedToConnect`
on a failed provider call, with an explicit note that "retry-policy
evaluation is a later checkpoint's job." This is that checkpoint —
`dialer_worker._place_call`'s failure branch now calls the same
`RecoveryManager`, using `retry_policy.never_connected_rules` instead
of `mid_call_rules`. One manager, two entry points (orchestrator for
mid-call, dialer worker for never-connected), matching the checkpoint's
own "keep these concepts separate" instruction for the two reason
taxonomies while sharing one retry *decision* engine.

## Campaign-paused retries: re-checked, not terminalized

Per Step 11, a paused campaign must not terminalize a retry that's
already been decided and scheduled — it should simply not dial while
paused, and re-check when it becomes due. The dispatcher's answer to
"still paused" is to re-schedule the same job a short, fixed interval
later (`RECOVERY_PAUSE_RECHECK_SECONDS`, default 60s) rather than
drop it — this is bounded, observable (a `RECOVERY_SKIPPED` event each
time), and requires no pub/sub "wake me when the campaign resumes"
mechanism that doesn't exist. It is not the same thing as an infinite
retry: no new `CallAttempt` or provider call is ever created by a
reschedule, only the existing scheduling entry moves further out.

## Calling-window closed: reschedule to the window's reopening

Same reasoning as campaign pause — Step 10 explicitly says not to
dial outside the window but also not to permanently fail the contact
over it. The dispatcher computes the next valid `window_start` and
reschedules to exactly that time (not a fixed poll interval), so a
contact due at 9pm with a 10am-6pm window is retried once, right at
10am the next valid day, not polled every minute overnight.
