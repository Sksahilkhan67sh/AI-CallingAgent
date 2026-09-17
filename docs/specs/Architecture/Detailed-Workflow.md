# AI Calling Agent — Detailed Workflow

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** PRD v1.0 · FRS v1.0 · User Stories v1.0 · Acceptance Criteria v1.0 · System Architecture v1.0 · Workflow diagram v1.0

**Purpose:** this document walks through the exact sequence of events for a contact, step by step, from import to final output — including every decision point and branch. Where a step maps to a functional requirement, its FR ID is noted in brackets.

---

## Stage 1 — Calling system

**Goal:** turn a raw contact list into connected calls.

**Step 1.1 — Import.** A campaign manager uploads a CSV or Excel file. Each row is validated as a phone number; malformed rows are rejected. `[FR-1.1, FR-1.2]`

**Step 1.2 — Deduplicate.** Duplicate phone numbers within the same import are collapsed to a single contact. `[FR-1.3]`

**Step 1.3 — Campaign creation.** A campaign record is created. Every surviving contact is set to status `Pending`. `[FR-1.4]`

**Step 1.4 — Queue scheduling.** Contacts enter the calling queue. The queue respects the campaign's configured call rate and the calling window (default 10 AM – 6 PM). `[FR-1.5, FR-6.6]`

**Step 1.5 — Dial.** When a contact's turn arrives, the telephony provider dials the number. `[FR-1.6]`

**Step 1.6 — Decision: Call connected?**

- **Yes →** proceed to Stage 2, Step 2.1.
- **No →** proceed to Step 1.7.

**Step 1.7 — Classify the failure.** The reason is recorded as one of: no answer, busy, invalid number, call rejected, network error, provider error. `[FR-1.7]`

**Step 1.8 — Update status & schedule retry.** The attempt is marked, and the shared retry policy (see [Retry Policy](#retry-policy)) is checked. `[FR-1.8]`

- If the reason is retryable per the `NEVER_CONNECTED_FAILURE_REASON` retry defaults (`no_answer`, `busy`, `network_error`, `provider_error` — see [Retry Policy](#retry-policy)) and the max-retry count has not been reached, the contact's attempt count is incremented and it is placed back into the **calling queue** — the same queue from Step 1.4, not back into Step 1.1's import. `[FR-1.9]`
- If the reason is `rejected` or `invalid_number` (not retryable by default), or the max-retry count has been reached, no further retry is scheduled. The contact is closed with its last known status. It does not produce a Final Output record, since no conversation occurred.

---

## Stage 2 — AI conversation engine

**Goal:** hold a real-time voice conversation for as long as the call is connected.

**Step 2.1 — Listen.** The contact's speech is captured and transcribed to text in real time. Silence is detected to mark the end of an utterance. `[FR-2.1, FR-2.2]`

**Step 2.2 — Understand.** The transcribed utterance is interpreted for intent and entities, using the conversation memory built up so far in the call. The next action is decided against the campaign's configured script and goals. `[FR-2.3, FR-2.4]`

**Step 2.3 — Respond.** A natural-sounding reply is generated and spoken. If the contact interrupts, the system detects this and adapts. `[FR-2.5, FR-2.6]`

**Step 2.4 — Loop.** Steps 2.1–2.3 repeat continuously. `[FR-2.7]`

**Step 2.5 — Decision: does the call end normally, or disconnect unexpectedly?**

- **Ends normally** (contact hangs up in the ordinary course of the conversation, or the script reaches its natural conclusion) → proceed to Stage 3, Step 3.1.
- **Disconnects unexpectedly** while the loop is still active → proceed to Stage 4, Step 4.1. `[FR-2.8]`

---

## Stage 3 — Data & admin

**Goal:** turn a finished call into stored, actionable data.

**Step 3.1 — Post-call analysis.** The full transcript is analyzed: interest classification, lead score, key feedback. `[FR-3.1, FR-3.2]`

**Step 3.2 — Persist.** The recording, transcript, and analysis are saved to the database, and the contact's attempt history is updated. `[FR-3.3, FR-3.4]`

**Step 3.3 — Surface.** The admin dashboard reflects the updated campaign status, lead classification, and analytics. `[FR-3.5]`

**Step 3.4 — Proceed.** The call proceeds to Stage 5, Step 5.1.

*(Note: the admin dashboard is also where the retry policy is configured — see [Retry Policy](#retry-policy) — but this is a configuration action, independent of any single call's progression through the stages.)* `[FR-3.6]`

---

## Stage 4 — Call disconnect & recovery flow

**Goal:** recover a dropped call wherever possible, and close it out cleanly when it can't be recovered. This stage runs only for a call that *connected* and then dropped — it is a separate scenario from Stage 1's never-connected handling, and the two never share a decision point.

**Step 4.1 — Save current state immediately.** Before anything else happens, the system saves:
- the partial transcript captured so far
- the recording captured so far
- the accumulated conversation memory
- call status is set to `Disconnected`
`[FR-4.1, FR-4.2]`

**Step 4.2 — Detect disconnect reason.** The cause is classified as one of: technical issue, network problem, customer hangup, provider error, AI error, or unknown. `[FR-4.3]`

**Step 4.3 — Decision: should retry?** The classified reason and the contact's current attempt count are checked against the shared retry policy. `[FR-4.4]`

- **Yes →** proceed to Step 4.4.
- **No →** proceed to Step 4.6.

**Step 4.4 — Schedule retry.** Retry rules are applied, the next call time is set, the contact is added back to the calling queue for redial, the attempt count is incremented, and the conversation ID is kept so memory can be reloaded. `[FR-4.4]`

**Step 4.5 — Reconnect call.** The system dials again, loads the previous conversation memory, and greets the contact with an acknowledgment of the disconnect (for example: *"Sorry for the disconnection — we were just discussing… let's continue from there."*). The conversation then resumes **inside Stage 2's loop**, from Step 2.1, using the reloaded context — it does not restart as a fresh, context-free call. `[FR-4.5, FR-4.6, FR-4.7]`

**Step 4.6 — Mark as completed (partial).** The call is marked `Completed (Partial)`:
- what was captured is saved (already done in Step 4.1)
- post-call analysis still runs, on the partial transcript (same process as Step 3.1)
- a disposition is recorded (e.g. "Customer Hung Up")
- no further retry is attempted
`[FR-4.8, FR-4.9, FR-4.10]`

**Step 4.7 — Proceed.** The call proceeds to Stage 5, Step 5.1 — the same destination as a normal completion (Stage 3).

---

## Stage 5 — Final output

**Goal:** produce one usable record per contact, no matter how the call ended.

**Step 5.1 — Generate the output record.** Triggered by either Step 3.4 (normal completion) or Step 4.7 (recovered or partial completion). `[FR-5.1, FR-5.2]`

**Step 5.2 — Populate output fields.** `[FR-5.3]`

| Field | Contents |
|---|---|
| Full transcript | Complete or partial text, timestamped, across every attempt |
| Recording | Audio file per attempt, playable or downloadable |
| Feedback | Customer feedback, key points, sentiment, objections, suggestions |
| Interest detection | Interested / not interested, follow-up detection, buying intent, intent confidence |
| Lead score | 0–100 score, qualification level, hot / warm / cold, conversion probability |
| Call summary | AI-generated summary, next-action suggestion, final disposition, tags/notes |
| Contact view (admin) | All call attempts, timeline, recordings, transcript & analysis, manual notes |

**Step 5.3 — Done.** The record is available to the sales/follow-up team and visible in the admin contact view.

---

## Retry policy

Referenced by Step 1.8 (never-connected) and Step 4.3 (mid-call disconnect). One shared configuration, evaluated independently against the appropriate taxonomy in each case (Call State Machine §5.1–§5.2). `[FR-6.1–FR-6.7]`

| Setting | Default value |
|---|---|
| Max retries | 2 retries per contact (3 total dial attempts: initial + 2 retries) |
| Retry spacing | Retry #1 at 30 seconds → Retry #2 at 10 minutes (escalating) |
| Calling window | 10 AM – 6 PM |
| Configuration | Managed from the admin dashboard, effective immediately, no deployment required |

**Retry-on-reason, by taxonomy** (default values — configurable per campaign; see Call State Machine §5.1–§5.2 for rationale):

| Never-connected reason (Step 1.7) | Retry? | | Mid-call disconnect reason (Step 4.2) | Retry? |
|---|---|---|---|---|
| `no_answer` | Yes | | `technical_issue` | Yes |
| `busy` | Yes | | `network_problem` | Yes |
| `network_error` | Yes | | `provider_error` | Yes |
| `provider_error` | Yes | | `ai_error` | Yes |
| `invalid_number` | No | | `unknown` | Yes |
| `rejected` | No | | `customer_hangup` | No |

---

## End-to-end path summary

Five possible journeys through the workflow, all shown for completeness:

1. **Straight through:** Step 1.1 → 1.6 (Yes) → Stage 2 loop → 2.5 (ends normally) → Stage 3 → Stage 5.
2. **Never connects, retried, then succeeds:** Step 1.6 (No) → 1.7 → 1.8 (retry) → back to Step 1.4 → eventually 1.6 (Yes) → journey 1 continues.
3. **Never connects, exhausted:** Step 1.6 (No) → 1.7 → 1.8 (no retry) → closed, no Stage 5 record.
4. **Connects, drops, recovers:** Journey 1 up to Stage 2 → 2.5 (disconnects) → Stage 4 → 4.3 (Yes) → 4.5 → back into Stage 2 loop → eventually 2.5 (ends normally) → Stage 3 → Stage 5.
5. **Connects, drops, not recovered:** Journey 1 up to Stage 2 → 2.5 (disconnects) → Stage 4 → 4.3 (No) → 4.6 → Stage 5 (partial record).

---

*No lost conversations. More opportunities. Higher conversion.*
