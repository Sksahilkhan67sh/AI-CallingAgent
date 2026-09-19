# Checkpoint 03 — implementation notes / deviations

## Queue technology: Redis Streams

`docs/specs/Architecture/Infrastructure-Architecture.md` names the
queue generically ("Call Queue — message broker") without committing to
a specific technology. The checkpoint's own instructions say to use
Redis if Redis-based queueing is appropriate, and list Redis as
suitable for "durable queue, hot state, distributed locks, rate
limiting, concurrency counters." Implemented as a single Redis Stream
(`calls:outbound`) with one consumer group (`dialer-workers`) —
Streams give consumer-group claiming, at-least-once delivery via
`XREADGROUP`/`XACK`, and crash recovery via `XPENDING`/`XCLAIM` natively,
without a second broker.

One stream, not one per campaign: campaign isolation (Step 11) is
enforced by the admission controller's per-campaign counters, not by
routing to separate streams — N streams for N campaigns doesn't scale
and isn't needed for isolation.

## `call_attempt.provider` / `call_attempt.provider_call_id` added

Not in the reconciled `Database-Design.md` §2.5 schema. Required by
this checkpoint's own Step 19 (reconciling an ambiguous provider
outcome means querying the provider by its call ID) and Step 15
(idempotent claiming needs a way to tell "attempt row exists but no
provider call was ever placed" from "attempt row exists and a provider
call is already in flight or done"). The reconciled schema predates
provider integration; this is the schema catching up to a genuine new
requirement, not a speculative addition — see migration
`alembic/versions/<id>_add_provider_fields_to_call_attempt.py`.

## CPS / concurrency limits: configuration, not new DB columns

No campaign-level or provider-level CPS/concurrency field exists
anywhere in the reconciled schema (`Campaign` has no such column, and
Checkpoint 02 didn't add one). Rather than add speculative schema for
numbers nothing else reads yet, global/campaign/provider CPS and
concurrency limits are `Settings` (environment configuration) — the
same numeric limit applied independently per campaign and per provider
via separate Redis counter keys, which is what actually delivers
Step 11's "campaign isolation" (each campaign's counter is independent,
so one campaign hitting its limit doesn't affect another) without
requiring a config UI or per-campaign override that doesn't exist yet.

## Contact status after a call outcome: partial, and that's intentional

Per Step 18, explicit rejection must not automatically create a retry,
and per the checkpoint's own framing, full retry/recovery execution is
a later checkpoint's job. So this checkpoint drives `Contact.status`
only as far as this checkpoint's scope goes:

- `Pending` → `Dialing` when a worker claims the job and is about to
  dial (matches `Call-State-Machine.md`'s own `Pending -> Dialing`
  transition).
- `Dialing` → `InConversation` on a `Connected` outcome — correct per
  the state machine, and the natural hand-off point to the conversation
  layer a later checkpoint builds.
- On a `FailedToConnect` outcome, the contact **stays at `Dialing`**.
  The state machine's own next step from there is `RetryScheduled`,
  decided by retry-policy evaluation (max retries, per-reason
  eligibility, spacing) — logic this checkpoint explicitly must not
  implement. Leaving the contact at `Dialing` rather than inventing an
  interim status is the honest reflection of "an attempt was made, and
  what happens next is a future checkpoint's decision" — a stray
  `Dialing` contact is exactly what the Recovery/retry checkpoint
  should pick up and move to `RetryScheduled` or `Closed`.

## Provider adapter: mock only

No real provider is named in the reconciled spec (it says "Telephony
Provider (external)" generically), and no provider credentials exist
in this environment. Per this checkpoint's own Step 13, a mock/test
adapter is the correct choice here — implementing a real Twilio/Plivo
adapter with no credentials to exercise it would mean shipping
integration code no test in this environment could actually verify,
which is worse than not shipping it. `TelephonyProvider` is an
abstract contract; `MockTelephonyProvider` is the only concrete
implementation, selected via `TELEPHONY_PROVIDER=mock` (the only
supported value right now). Swapping in a real adapter later is a
matter of implementing the same contract, not touching any calling
code.

## Webhook signature verification: shared-secret placeholder

`Webhook-Specification.md`'s full flow (signature verification →
schema validation → replay protection → idempotency → state transition
→ DB transaction → ACK) is provider-specific for the signature step,
and there's no real provider to match a real scheme against. A shared-
secret header (`X-Webhook-Secret`, compared against
`Settings.telephony_webhook_secret`) stands in for real signature
verification until a real provider is integrated — every other stage
(idempotency via the existing `processed_event` table, minimal schema
validation, state update) is real. Only call-initiation/status fields
are handled; no conversation events, per this checkpoint's explicit
scope.

## Concurrency-counter crash safety: a known, documented limit

Active-call concurrency counters (Redis `INCR`/`DECR`) are released in
a `try/finally` around the dial-and-persist step, so any Python-level
exception still releases the slot. A hard process/host crash between
`INCR` and the `finally` could leak a slot. Full crash-safe reconciliation
(e.g. TTL'd membership + a reaper) is a reasonable next step but isn't
built here — it's exactly the kind of infrastructure-failure handling
`Infrastructure-Architecture.md` §10 assigns to later, standard recovery
handling, and adding it now would be speculative for a foundation
checkpoint. Documented rather than silently accepted.
