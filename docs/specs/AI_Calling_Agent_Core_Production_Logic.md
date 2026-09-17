# AI Calling Agent — Core Production Logic

> **Purpose:** This document defines the actual runtime logic of the AI Calling Agent.
> It is intentionally focused on **states, decisions, transitions, retries, recovery, idempotency, and failure handling** — not the technology approach.

---

# 1. Master Logic

```text
CONTACT
   ↓
VALIDATE
   ↓
SUPPRESSION CHECK (Database Design §2.12 — single canonical DNC/opt-out check)
   ↓
ELIGIBILITY
   ↓
QUEUE
   ↓
CAPACITY CHECK
   ↓
DIAL
   ↓
CONNECTED?
   │
   ├── NO → CLASSIFY → RETRY POLICY → QUEUE / TERMINAL
   │
   └── YES
         ↓
      AI SESSION
         ↓
   STT → MEMORY → LLM → POLICY → TTS
         ↓
      CHECKPOINT
         ↓
      CONTINUE
         │
         ├── OBJECTIVE COMPLETE → TERMINAL
         │
         ├── OPT-OUT → SUPPRESSION WRITE → TERMINAL
         │
         ├── REJECTION → NO RETRY → TERMINAL
         │
         └── DISCONNECT
                ↓
          SAVE CHECKPOINT
                ↓
          DETECT REASON
                ↓
          RETRY POLICY
             ┌──┴──┐
            YES    NO
             ↓      ↓
        RETRY QUEUE TERMINAL
             ↓
          RECONNECT
             ↓
        LOAD MEMORY
             ↓
          RESUME
             ↓
          TERMINAL
             ↓
       POST-CALL ANALYSIS
             ↓
       PYTHON INTELLIGENCE
             ↓
      DATABASE + STORAGE
             ↓
          DASHBOARD
```

---

# 2. Contact Creation Logic

```text
CONTACT IMPORT
      ↓
VALIDATE
      │
      ├── INVALID → INVALID
      │
      └── VALID
           ↓
       NORMALIZE
           ↓
       DEDUPLICATE
           │
           ├── DUPLICATE → MERGE / SKIP
           │
           └── UNIQUE
                 ↓
             SAVE CONTACT
```

A contact must not enter the dialing queue directly after import.

---

# 3. Compliance / Eligibility Logic

```text
CONTACT
   ↓
SUPPRESSION CHECK (Database Design §2.12 — checks phone_number and contact_id)
   │
   ├── SUPPRESSED → STOP
   │
   └── NOT SUPPRESSED
          ↓
     CALLING WINDOW CHECK
                 │
                 ├── OUTSIDE WINDOW → SCHEDULE LATER
                 │
                 └── INSIDE WINDOW
                         ↓
                   CAMPAIGN ACTIVE?
                         │
                         ├── NO → WAIT
                         │
                         └── YES
                               ↓
                             QUEUE
```

---

# 4. Queue Admission Logic

A queued contact is not automatically allowed to dial.

```text
QUEUE JOB
    ↓
CAMPAIGN ACTIVE?
    │
    ├── NO → WAIT
    │
    └── YES
          ↓
       DNC CHECK AGAIN (suppression table — Database Design §2.12)
          │
          ├── SUPPRESSED → CANCEL JOB
          │
          └── CLEAR
                ↓
        CALLING WINDOW CHECK
                │
                ├── OUTSIDE → RESCHEDULE
                │
                └── INSIDE
                      ↓
               CAPACITY CHECK
                      │
              ┌───────┴────────┐
              ↓                ↓
         CAPACITY OK       CAPACITY FULL
              ↓                ↓
            CLAIM            WAIT
              ↓
             DIAL
```

The second suppression check is intentional because a contact can opt out after the job was created.

---

# 5. Capacity / Admission Logic

The admission controller checks:

```text
Global CPS
Campaign CPS
Provider CPS
Current concurrency
Campaign concurrency
Provider health
Destination/region rules
Calling window
```

Logic:

```text
REQUEST DIAL
     ↓
PROVIDER HEALTHY?
     │
     ├── NO → WAIT / FAILOVER POLICY
     │
     └── YES
          ↓
       CPS AVAILABLE?
          │
          ├── NO → WAIT
          │
          └── YES
                ↓
       CONCURRENCY AVAILABLE?
                │
                ├── NO → WAIT
                │
                └── YES
                      ↓
                    DIAL
```

**Never flood the telephony provider just because the queue is large.**

---

# 6. Call Attempt Creation

Every dial is a distinct attempt.

```text
CONTACT
   ↓
CREATE ATTEMPT
   ↓
attempt_id = UUID
   ↓
attempt_number = N
   ↓
idempotency_key
   =
campaign_id + contact_id + attempt_number
   ↓
CHECK DUPLICATE
   │
   ├── EXISTS → DO NOT CREATE SECOND ATTEMPT
   │
   └── NEW → CREATE
              ↓
             DIAL
```

---

# 7. Call Creation Logic

```text
DIAL REQUEST
     ↓
PROVIDER REQUEST
     │
     ├── SUCCESS
     │     ↓
     │   SAVE PROVIDER CALL ID
     │     ↓
     │   RINGING
     │
     ├── FAILURE
     │     ↓
     │   CLASSIFY ERROR
     │     ↓
     │   RETRY POLICY
     │
     └── TIMEOUT / UNKNOWN
           ↓
      PROVIDER STATUS CHECK
           │
        ┌──┴──────┐
        ↓         ↓
     EXISTS    NOT FOUND
        ↓         ↓
    CONTINUE    SAFE RETRY
```

Never blindly retry an ambiguous provider request.

---

# 8. Call Connection Logic

Classifies as `NEVER_CONNECTED_FAILURE_REASON` (Call State Machine §5.1) — distinct from `MID_CALL_DISCONNECT_REASON` (§17 below), which applies only after a call has connected.

```text
RINGING
   ↓
CALL CONNECTED?
   │
   ├── NO
   │    ├── NO_ANSWER
   │    ├── BUSY
   │    ├── INVALID_NUMBER
   │    ├── REJECTED
   │    ├── NETWORK_ERROR
   │    └── PROVIDER_ERROR
   │
   │         ↓
   │    CLASSIFY OUTCOME
   │         ↓
   │    RETRY POLICY (never_connected_rules — Database Design §2.2)
   │
   └── YES
        ↓
    CREATE CONVERSATION SESSION
        ↓
    LOAD INITIAL CONTEXT
        ↓
    START AI SESSION
```

---

# 9. Real-Time Conversation Logic

```text
CUSTOMER SPEAKS
      ↓
STREAMING STT
      ↓
TRANSCRIPT UPDATE
      ↓
MEMORY UPDATE
      ↓
INTENT / STATE DETECTION
      ↓
LLM
      ↓
POLICY VALIDATION
      ↓
RESPONSE
      ↓
TTS
      ↓
CUSTOMER HEARS
      ↓
CHECK OBJECTIVE
```

If objective is not complete:

```text
CHECK OBJECTIVE
      ↓
NOT COMPLETE
      ↓
LISTEN AGAIN
```

This forms the conversation loop.

---

# 10. Conversation State

The conversation should maintain structured state such as:

```json
{
  "conversation_id": "...",
  "call_id": "...",
  "attempt_id": "...",
  "stage": "INTEREST_CHECK",
  "intent": "INTERESTED",
  "customer_facts": [],
  "last_question": "...",
  "next_action": "..."
}
```

The exact fields can evolve, but state must be explicit and recoverable.

---

# 11. Transcript vs Memory

## Transcript

Chronological record:

```text
Customer: ...
Agent: ...
Customer: ...
Agent: ...
```

## Memory

Structured context:

```text
stage
intent
customer facts
conversation objective
last confirmed state
next action
```

They must not be treated as the same object.

---

# 12. Continuous Checkpoint Logic

Do not wait until the call ends.

```text
CUSTOMER TURN
     ↓
TRANSCRIPT
     ↓
MEMORY UPDATE
     ↓
CHECKPOINT
     ↓
NEXT TURN
```

Checkpoint after meaningful:

- Customer responses
- Agent responses
- Intent changes
- Stage changes
- Tool calls
- Business decisions
- Important customer facts

---

# 13. Customer Intent Logic

The AI may classify structured intent:

```text
INTERESTED
NOT_INTERESTED
REQUEST_CALLBACK
QUESTION
OPT_OUT
UNCLEAR
```

But:

```text
AI classification
      ↓
POLICY ENGINE
      ↓
DETERMINISTIC ACTION
```

The LLM should not directly perform infrastructure operations.

---

# 14. Opt-Out Logic

```text
CUSTOMER SAYS STOP
       ↓
OPT-OUT DETECTED (requires_suppression = true, Prompt Specification §4)
       ↓
WRITE SUPPRESSION ROW (Database Design §2.12 — source = agent_in_call)
       ↓
CANCEL FUTURE QUEUED JOBS
       ↓
CANCEL SCHEDULED RETRIES
       ↓
OPTIONAL CONFIRMATION SMS
       ↓
TERMINAL
```

Every future dialing attempt must check `suppression` again (final pre-dial eligibility check — Security Architecture §8). There is no separate consent table: `suppression` is the single canonical DNC/opt-out record (Database Design §2.12, Data-Privacy.md §9).

---

# 15. Explicit Rejection Logic

This is `NEVER_CONNECTED_FAILURE_REASON.rejected` (§8) — the call never connected, so no conversation occurred and no analysis runs (Call State Machine §2.3: `Closed` produces no Final Output record).

```text
CALL REJECTED (device/carrier level)
          ↓
       REJECTED
          ↓
      NO AUTO-RETRY (default)
          ↓
        TERMINAL (Closed — no Final Output record)
```

---

# 16. Disconnect Logic

```text
CALL ACTIVE
    ↓
DISCONNECT
    ↓
IMMEDIATELY SAVE STATE
    ↓
SAVE:
- Partial transcript
- Memory
- Last state
- Call metadata
- Disconnect timestamp
    ↓
DETECT DISCONNECT REASON
```

Possible reasons (`MID_CALL_DISCONNECT_REASON` — Call State Machine §5.2; exactly one of these six, no others):

```text
TECHNICAL_ISSUE
NETWORK_PROBLEM
CUSTOMER_HANGUP
PROVIDER_ERROR
AI_ERROR
UNKNOWN
```

An opt-out spoken mid-call is handled by §14 (Opt-Out Logic) as a `requires_suppression` capture, independent of `disconnect_reason` classification — it is not itself a disconnect reason. A rejected/never-connected call is handled by §8 (Call Connection Logic) under `NEVER_CONNECTED_FAILURE_REASON.rejected`, not here.

---

# 17. Disconnect Reason Logic

Branches on `MID_CALL_DISCONNECT_REASON` only (§16). Opt-out and never-connected rejection are handled separately (§14, §8) and never enter this classification.

```text
DETECT REASON
     │
     ├── TECHNICAL_ISSUE
     │      ↓
     │    RETRY
     │
     ├── NETWORK_PROBLEM
     │      ↓
     │    RETRY
     │
     ├── PROVIDER_ERROR
     │      ↓
     │    RETRY
     │
     ├── AI_ERROR
     │      ↓
     │    RETRY
     │
     ├── CUSTOMER_HANGUP
     │      ↓
     │  POLICY CHECK (default: no retry)
     │
     └── UNKNOWN
            ↓
          RETRY (conservative default)
```

---

# 18. Retry Decision Logic

"Retry allowed?" is evaluated against `never_connected_rules` (§8) or `mid_call_rules` (§17), whichever taxonomy applies to this failure — never mixed (Call State Machine §5.1–§5.2).

```text
FAILURE / DISCONNECT
        ↓
RETRY ALLOWED?
    ┌───┴────┐
    NO       YES
    ↓         ↓
TERMINAL   RETRY COUNT
              ↓
         MAX RETRIES?
          ┌──┴──┐
         YES    NO
          ↓      ↓
       TERMINAL  DELAY
                  ↓
             RETRY QUEUE
```

---

# 19. Retry Schedule

Canonical default (`max_retries = 2`, `retry_spacing_seconds = [30, 600]` — Database Design §2.2):

```text
Attempt 1 (initial)
   ↓
Failure
   ↓
Retry #1 → 30 seconds

Attempt 2
   ↓
Failure
   ↓
Retry #2 → 10 minutes

Attempt 3
   ↓
Failure
   ↓
MAX RETRIES REACHED
   ↓
TERMINAL
```

Retry limits and intervals must be configuration-driven. `retry_spacing_seconds` must always have exactly `max_retries` entries — raising `max_retries` requires adding a matching spacing value, not leaving an attempt with no configured delay.

---

# 20. Retry Re-Admission

A retry must not bypass normal admission controls.

```text
RETRY DUE
    ↓
RETRY QUEUE
    ↓
SUPPRESSION CHECK (Database Design §2.12 — single canonical DNC/opt-out check)
    ↓
CALLING WINDOW
    ↓
CAMPAIGN ACTIVE
    ↓
CAPACITY CHECK
    ↓
DIAL
```

This prevents a previously valid contact from bypassing newly changed rules.

---

# 21. Reconnect / Resume Logic

A successful retry creates a **new call attempt**, but can use the previous conversation context.

```text
RETRY CALL
    ↓
CONNECTED
    ↓
LOAD PREVIOUS CONVERSATION
    ↓
LOAD LAST CHECKPOINT
    ↓
VALIDATE CONTEXT
    ↓
RESUME CONVERSATION
```

Do not unnecessarily restart the entire conversation.

---

# 22. Worker Crash Logic

```text
WORKER CLAIMS JOB
       ↓
WORKER CRASHES
       ↓
JOB NOT ACKNOWLEDGED
       ↓
QUEUE RECOVERY
       ↓
ANOTHER WORKER CLAIMS JOB
       ↓
LOAD DATABASE STATE
       ↓
IDEMPOTENCY CHECK
       ↓
CONTINUE / SKIP
```

---

# 23. Job Acknowledgement Logic

Correct:

```text
GET JOB
   ↓
CLAIM
   ↓
VALIDATE
   ↓
EXECUTE
   ↓
PERSIST REQUIRED RESULT
   ↓
EMIT EVENT
   ↓
ACK
```

Avoid acknowledging a critical job before its required durable state has been persisted.

---

# 24. Webhook Logic

```text
WEBHOOK RECEIVED
       ↓
VERIFY SIGNATURE
       ↓
VALIDATE PAYLOAD
       ↓
CHECK TIMESTAMP / REPLAY
       ↓
CHECK event_id
       │
       ├── ALREADY PROCESSED → NO-OP
       │
       └── NEW
            ↓
       VALIDATE STATE TRANSITION
            ↓
       DATABASE TRANSACTION
            ↓
       SAVE event_id
            ↓
           ACK
```

---

# 25. Duplicate Webhook Logic

If the same event arrives multiple times:

```text
CALL_CONNECTED
CALL_CONNECTED
CALL_CONNECTED
```

Result:

```text
First  → PROCESS
Second → NO-OP
Third  → NO-OP
```

Webhook duplication must never create duplicate calls, attempts, messages, or analysis jobs.

---

# 26. Post-Call Terminal Logic

A call becomes terminal when the attempt can no longer continue. This system distinguishes four kinds of field, and they must not be conflated:

- **STATE** — the canonical Contact/Call Attempt lifecycle position (Call State Machine §2.1, §3.1). This is the only thing that drives what happens next in the workflow.
- **OUTCOME/REASON** — why a given state was reached: `connection_failure_reason` (§8) or `disconnect_reason` (§16-§17). An attribute *on* a state, never a state itself.
- **ANALYSIS RESULT** — output of Post-Call Analysis (§27): disposition text, interest classification, lead score. Only produced for attempts that reached `Connected`.

```text
CALL ENDS
    ↓
SAVE FINAL STATE
    ↓
MARK ATTEMPT TERMINAL (Call Attempt STATE — Call State Machine §3.1)
    ↓
EMIT CALL_COMPLETED / TERMINAL EVENT
    ↓
ANALYSIS QUEUE (only if the attempt reached Connected — see below)
```

**Call Attempt terminal STATEs** (Call State Machine §3.1 — exactly one of these, always):

```text
FailedToConnect   — carries connection_failure_reason: no_answer | busy | invalid_number | rejected | network_error | provider_error (§8)
DroppedMidCall    — carries disconnect_reason: technical_issue | network_problem | provider_error | ai_error | unknown | customer_hangup (§16-§17)
EndedNormally     — no reason field; conversation completed cleanly
```

**Contact terminal STATEs** (Call State Machine §2.1 — reached once the attempt(s) resolve):

```text
Completed         — attempt ended normally; has ANALYSIS RESULT + Final Output
CompletedPartial  — attempt(s) dropped mid-call, no further retry; has ANALYSIS RESULT (on the partial transcript) + Final Output
Closed            — attempt(s) never connected, retries exhausted/disallowed; NO analysis, NO Final Output (Call State Machine §2.3)
```

`OPTED_OUT` is not a state — it is `contact_history.suppression_flag = true` (Database Design §2.11), set whenever `requires_suppression` was captured on any attempt, independent of which terminal state that attempt reached. `TECHNICAL_FAILURE` / `AI_FAILURE` / `PROVIDER_FAILURE` are not states either — they are `disconnect_reason` (mid-call) or `connection_failure_reason` (never-connected) values, already covered above; do not introduce them as separate terminal states.

---

# 27. Post-Call Analysis Logic

```text
TERMINAL CALL
      ↓
ANALYSIS QUEUE
      ↓
PYTHON WORKER
      ↓
TRANSCRIPT PROCESSING
      ↓
SUMMARY
      ↓
FEEDBACK
      ↓
INTENT
      ↓
INTEREST
      ↓
SENTIMENT
      ↓
LEAD SCORE
      ↓
NEXT ACTION
      ↓
SAVE RESULTS
```

Analysis must never trigger an automatic redial by itself.

---

# 28. Analysis Failure Logic

```text
CALL COMPLETED
      ↓
ANALYSIS
      ↓
FAIL
      ↓
ANALYSIS RETRY QUEUE
      ↓
PYTHON WORKER
```

Important:

```text
Analysis Failure ≠ Call Failure
```

Do not call the customer again just because analysis failed.

---

# 29. Campaign Pause Logic

```text
ADMIN → PAUSE CAMPAIGN
              ↓
       STOP NEW ADMISSION
              ↓
       QUEUED JOBS WAIT
              ↓
       ACTIVE CALLS FINISH
              ↓
        CAMPAIGN PAUSED
```

Resume:

```text
RESUME
  ↓
ELIGIBILITY CHECK
  ↓
QUEUE
  ↓
CAPACITY CHECK
  ↓
DIAL
```

---

# 30. Emergency Kill Switch

```text
STOP ALL OUTBOUND
        ↓
STOP JOB ADMISSION
        ↓
STOP NEW DIALS
        ↓
APPLY ACTIVE-CALL POLICY
        ↓
AUDIT EVENT
        ↓
ADMIN ALERT
```

The kill switch must be independent of normal campaign controls.

---

# 31. Provider Failure Logic

```text
PROVIDER ERROR RATE RISES
        ↓
HEALTH STATUS = DEGRADED
        ↓
CIRCUIT BREAKER
        ↓
REDUCE / STOP NEW TRAFFIC
        ↓
OPTIONAL PROVIDER FAILOVER
```

Failover must respect campaign, region, provider and compliance configuration.

---

# 32. AI Failure Logic

```text
AI REQUEST
    ↓
TIMEOUT / FAILURE
    ↓
BOUNDED RETRY
    ↓
OPTIONAL FALLBACK
    ↓
SAFE RESPONSE
    │
    └── IF UNSAFE / IMPOSSIBLE
             ↓
        GRACEFUL TERMINATION
             ↓
           ANALYSIS
```

Never keep the customer waiting indefinitely.

---

# 33. Redis Failure Logic

Redis should not be the permanent source of truth.

```text
REDIS FAILURE
      ↓
STOP UNSAFE NEW WORK
      ↓
PROTECT DURABLE DATABASE STATE
      ↓
RECOVER QUEUE / HOT STATE
      ↓
RECONCILE IN-FLIGHT WORK
      ↓
RESUME
```

Critical business state must remain recoverable from PostgreSQL.

---

# 34. Database Failure Logic

```text
DATABASE TRANSIENT FAILURE
        ↓
BOUNDED RETRY
        ↓
IF STILL FAILING
        ↓
STOP / BACKPRESSURE AFFECTED WORK
        ↓
ALERT
        ↓
RECOVER
        ↓
RECONCILE
```

Do not continue acknowledging work when durable state cannot be safely persisted.

---

# 35. Duplicate Call Protection

Before dialing:

```text
CAMPAIGN ACTIVE?
   ↓
SUPPRESSION CLEAR? (Database Design §2.12)
   ↓
CALLING WINDOW VALID?
   ↓
CONTACT ALREADY ACTIVE?
   ↓
ATTEMPT ALREADY EXISTS?
   ↓
PROVIDER AMBIGUOUS STATE?
   ↓
CAPACITY AVAILABLE?
   ↓
DIAL
```

If any unsafe condition is found:

```text
DO NOT DIAL
```

---

# 36. Campaign Isolation

Each campaign can have:

```text
CPS limit
Concurrency limit
Retry policy
Calling window
Provider configuration
Daily call limit
Budget limit
```

A large campaign must not automatically consume all platform capacity.

---

# 37. Cost Protection Logic

```text
CALL REQUEST
    ↓
CAMPAIGN BUDGET CHECK
    ↓
DAILY LIMIT CHECK
    ↓
PROVIDER SPEND CHECK
    ↓
AI/TTS BUDGET CHECK
    ↓
ALLOWED?
 ┌──┴──┐
YES    NO
 ↓      ↓
DIAL   PAUSE
        ↓
     ALERT ADMIN
```

---

# 38. Final End-to-End State Flow

```text
IMPORTED
   ↓
VALIDATING
   ↓
ELIGIBLE
   ↓
QUEUED
   ↓
DIALING
   ↓
RINGING
   │
   ├── NO_ANSWER → RETRY POLICY
   ├── BUSY → RETRY POLICY
   ├── NETWORK_ERROR → RETRY POLICY
   ├── PROVIDER_ERROR → RETRY POLICY
   ├── INVALID_NUMBER → TERMINAL
   ├── REJECTED → TERMINAL
   │
   └── CONNECTED
          ↓
    IN_CONVERSATION
          ↓
      CHECKPOINT
          ↓
      CONVERSATION
          │
          ├── OBJECTIVE COMPLETE
          │       ↓
          │    TERMINAL
          │
          ├── OPT_OUT
          │       ↓
          │    SUPPRESSION WRITE
          │       ↓
          │    TERMINAL
          │
          └── DISCONNECT
                  ↓
             SAVE STATE
                  ↓
             DETECT REASON
                  ↓
             RETRY POLICY
              ┌───┴───┐
             YES       NO
              ↓         ↓
        RETRY QUEUE   TERMINAL
              ↓         ↓
           RECONNECT  ANALYSIS
              ↓         ↓
        LOAD MEMORY  RESULTS
              ↓         ↓
           RESUME    DASHBOARD
              ↓
          TERMINAL
              ↓
          ANALYSIS
              ↓
           RESULTS
```

---

# 39. The Most Important Rules

```text
RULE 1
PostgreSQL = durable truth

RULE 2
Redis = queue/hot state, not permanent truth

RULE 3
Every call = explicit state machine

RULE 4
Every call = separate attempt

RULE 5
Every external event = verified + deduplicated

RULE 6
Every critical job = idempotent

RULE 7
Never blindly retry ambiguous provider outcomes

RULE 8
Suppression (DNC/opt-out) is checked before queue AND before dialing

RULE 9
Explicit rejection = no automatic retry

RULE 10
Opt-out = immediate suppression write + future retry cancellation

RULE 11
Conversation state is checkpointed continuously

RULE 12
Recovery Manager owns retry/resume decisions

RULE 13
Post-call analysis never causes a redial

RULE 14
Queue backpressure protects providers and the system

RULE 15
Calling, AI, recovery and analysis workers scale independently

RULE 16
Every critical action is observable and auditable

RULE 17
Campaign pause stops new calls

RULE 18
Emergency kill switch stops outbound admission globally

RULE 19
Budget limits prevent runaway calling/AI costs

RULE 20
Failure handling is part of the normal state machine
```

---

# 40. Final Logic

The system should behave as:

```text
SAFE
 ↓
ELIGIBLE
 ↓
QUEUED
 ↓
CONTROLLED
 ↓
DIALED
 ↓
CONNECTED
 ↓
CONVERSATION
 ↓
CHECKPOINTED
 ↓
COMPLETED
```

If something goes wrong:

```text
FAILURE
 ↓
CLASSIFY
 ↓
SAVE STATE
 ↓
DETERMINE SAFE ACTION
 ├── RETRY
 ├── RESUME
 ├── FALLBACK
 ├── WAIT
 └── TERMINATE
```

The fundamental principle is:

> **Never lose state, never create an uncontrolled duplicate call, never bypass compliance checks, never exceed controlled capacity, and never let one failed component bring down the entire calling system.**
