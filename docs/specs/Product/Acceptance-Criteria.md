# AI Calling Agent — Acceptance Criteria

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** PRD v1.0 · FRS v1.0 · User Stories v1.0 · Workflow diagram v1.0

**Format:** Given / When / Then, one block per user story. Story IDs match `User-Stories.docx`; requirement references match `FRS.docx` (`FR-x.y`).

---

## Epic 1 — Calling system

### US-1.1 — Import a contact list
*As a Campaign Manager, I want to import a contact list from CSV or Excel, so that I can start a calling campaign without manual entry.*
**Refs:** FR-1.1, FR-1.2, FR-1.3, FR-1.4

- **Given** a well-formed CSV or Excel file, **when** it is uploaded, **then** every row is parsed into a contact record.
- **Given** a row with an invalid or malformed phone number, **when** the file is imported, **then** that row is rejected and not added to the campaign.
- **Given** two rows with the same phone number in one import, **when** the file is processed, **then** only one contact record is created.
- **Given** the import completes with at least one valid contact, **when** the campaign is created, **then** every valid contact's status is set to `Pending`.
- **Given** an empty file or a file with zero valid rows, **when** imported, **then** no campaign is created and the manager is informed no valid contacts were found.

### US-1.2 — Set call rate and schedule
*As a Campaign Manager, I want to set the call rate and schedule for a campaign, so that calling stays within my desired pace and hours.*
**Refs:** FR-1.5, FR-6.6

- **Given** a campaign, **when** I set a maximum call rate, **then** the calling queue does not dispatch calls faster than that rate.
- **Given** a configured calling window (e.g. 10 AM – 6 PM), **when** the current time is outside that window, **then** no new calls are dialed.
- **Given** a call is queued just before the window closes, **when** the window closes before it is dialed, **then** it is deferred to the next open window.

### US-1.3 — Automatic dialing
*As the System, I want to dial each pending contact automatically, so that no call requires manual dialing.*
**Refs:** FR-1.6, FR-1.10

- **Given** a contact with status `Pending`, **when** its turn in the queue is reached, **then** the system dials it via the telephony provider without human action.
- **Given** a dial attempt succeeds, **when** the call connects, **then** control passes to the conversation engine (Epic 2).
- **Given** a dial attempt is in progress, **when** it is still connecting, **then** the contact's status reflects "dialing" rather than `Pending` or `Completed`.

### US-1.4 — Classify connection failures
*As the System, I want to detect why a call failed to connect, so that I can decide whether it is worth retrying.*
**Refs:** FR-1.7

- **Given** a dial attempt, **when** it fails, **then** the failure reason is recorded as exactly one of: `no answer`, `busy`, `invalid number`, `rejected`, `network error`, `provider error`.
- **Given** a failure reason cannot be determined, **when** logged, **then** it is not left blank — it is recorded under the closest applicable category or flagged for review.

### US-1.5 — Retry never-connected calls via the queue
*As the System, I want to return a failed-to-connect contact to the calling queue rather than to import, so that it is redialed without re-triggering campaign setup.*
**Refs:** FR-1.8, FR-1.9, FR-6.1–FR-6.5

- **Given** a failed connection with a reason retryable per the default `never_connected_rules` (`no_answer`, `busy`, `network_error`, `provider_error`), **when** the retry policy's max-retry count has not been reached, **then** the contact is returned to the calling queue with an incremented attempt count.
- **Given** a failed connection due to `rejected` or `invalid_number`, **when** evaluated, **then** no retry is scheduled by default, regardless of remaining retry budget.
- **Given** a contact has reached the maximum retry count, **when** another failure occurs, **then** no further retry is scheduled and the contact is marked closed with its last known status.
- **Given** a retry is scheduled, **then** it is placed back into the calling queue — **never** back into the contact import/database step.

---

## Epic 2 — AI conversation engine

### US-2.1 — Real-time speech understanding
*As a Contact, I want the AI agent to understand what I'm saying in real time, so that the conversation feels natural.*
**Refs:** FR-2.1, FR-2.2

- **Given** a connected call, **when** the contact speaks, **then** speech is transcribed to text with no perceptible lag in normal conditions.
- **Given** the contact stops speaking, **when** a silence threshold is reached, **then** the system treats the utterance as complete and proceeds to interpret it.
- **Given** background noise or overlapping speech, **when** transcribing, **then** the system does not indefinitely wait — it still detects an end-of-utterance within a bounded time.

### US-2.2 — Contextual interpretation
*As the System, I want to interpret intent and remember context from earlier in the call, so that my responses stay coherent.*
**Refs:** FR-2.3, FR-2.4

- **Given** a transcribed utterance, **when** interpreted, **then** intent and relevant entities are extracted.
- **Given** information provided earlier in the same call, **when** a later utterance references it, **then** the system's interpretation accounts for that earlier context.
- **Given** an interpreted utterance, **when** deciding the next action, **then** the decision follows the campaign's configured script and goals rather than an unconstrained response.

### US-2.3 — Natural, interruptible responses
*As a Contact, I want the AI agent to respond naturally and let me interrupt, so that the call doesn't feel robotic.*
**Refs:** FR-2.5, FR-2.6

- **Given** a decided response, **when** rendered, **then** it is spoken in natural-sounding speech (not a flat, robotic read).
- **Given** the agent is speaking, **when** the contact begins speaking at the same time, **then** the agent detects the interruption and stops speaking within a short, bounded delay.
- **Given** an interruption was handled, **when** the agent resumes, **then** it responds to the new input rather than continuing its interrupted sentence verbatim.

### US-2.4 — Continuous conversation loop
*As the System, I want the listen-understand-respond cycle to repeat continuously, so that the conversation continues until it naturally ends.*
**Refs:** FR-2.7, FR-2.8

- **Given** one listen → understand → respond cycle completes, **when** the call is still connected, **then** the next cycle begins automatically without manual restart.
- **Given** the contact ends the call normally, **when** detected, **then** the loop terminates and the call proceeds to post-call analysis (Epic 3).
- **Given** the call disconnects unexpectedly mid-loop, **when** detected, **then** the loop halts immediately and control passes to the disconnect & recovery flow (Epic 4) — not to post-call analysis directly.

---

## Epic 3 — Data & admin

### US-3.1 — Lead scoring
*As a Sales team member, I want a lead score and interest classification for every completed call, so that I can prioritise follow-up.*
**Refs:** FR-3.1, FR-3.2

- **Given** a call reaches a terminal state, **when** post-call analysis runs, **then** an interest classification (e.g. interested / not interested) is produced.
- **Given** the same analysis, **then** a numeric lead score between 0 and 100 is produced alongside a hot/warm/cold qualification tag.
- **Given** analysis cannot confidently classify interest, **when** this occurs, **then** the call is still scored, with a lower confidence value rather than a blocked or missing record.

### US-3.2 — Persisted call data
*As a Campaign Manager, I want every call's recording, transcript, and analysis stored, so that I can review it later.*
**Refs:** FR-3.3, FR-3.4

- **Given** analysis completes for a call, **when** saved, **then** the recording, full transcript, and analysis output are all stored against that contact's record.
- **Given** a contact has multiple attempts, **when** viewed, **then** each attempt's data is retained and distinguishable, not overwritten by the latest attempt.

### US-3.3 — Live campaign dashboard
*As an Admin, I want a live dashboard of campaign status and call analytics, so that I can monitor progress without querying the database directly.*
**Refs:** FR-3.5

- **Given** an active campaign, **when** the dashboard is opened, **then** current status counts (pending, in-progress, completed, failed) are visible.
- **Given** completed calls exist, **when** viewed on the dashboard, **then** lead classification and call analytics are shown and reflect the latest stored data.

### US-3.4 — Configure retry policy from the dashboard
*As an Admin, I want to configure the retry policy from the dashboard, so that I can adjust it without a deployment.*
**Refs:** FR-3.6, FR-6.7

- **Given** the dashboard's retry-policy settings, **when** a value is changed and saved, **then** subsequent retry decisions use the new value without any code deployment.
- **Given** an invalid value is entered (e.g. negative retry count), **when** submitted, **then** the change is rejected with a clear validation message.

---

## Epic 4 — Call disconnect & recovery flow

*Applies only when a call has connected and then disconnects unexpectedly — independent from Epic 1's never-connected handling.*

### US-4.1 — Preserve state on disconnect
*As a Contact who gets disconnected mid-call, I want everything I've already said to be preserved, so that I don't have to repeat myself if reconnected.*
**Refs:** FR-4.1, FR-4.2

- **Given** an active call, **when** it disconnects unexpectedly, **then** the partial transcript up to that point is saved.
- **Given** the same disconnect event, **then** the recording captured so far and the accumulated conversation memory are also saved, in the same operation as the transcript.
- **Given** the save completes, **when** checked, **then** the call's status is set to `Disconnected`.
- **Given** the disconnect happens at any point in the call (start, middle, or near the end), **then** the save behavior is identical — no partial-save gaps depending on timing.

### US-4.2 — Classify the disconnect reason
*As the System, I want to classify why a call disconnected, so that the retry decision reflects the actual cause.*
**Refs:** FR-4.3

- **Given** a disconnected call, **when** classified, **then** exactly one reason is recorded: `technical issue`, `network problem`, `provider error`, `customer hangup`, `AI error`, or `unknown`.
- **Given** the cause cannot be determined from available signals, **when** classified, **then** it is recorded as `unknown` rather than left blank or guessed incorrectly.

### US-4.3 — Evaluate retry eligibility
*As the System, I want to check the retry policy before deciding to reconnect, so that retries follow configured business rules.*
**Refs:** FR-4.4, FR-6.1–FR-6.6

- **Given** a classified disconnect, **when** evaluated against the retry policy, **then** a clear retry / no-retry decision is produced before any further action is taken.
- **Given** the disconnect reason is `customer hangup`, **when** evaluated, **then** the decision follows the same policy rules as any other reason type (no special-case bypass unless configured).
- **Given** the contact has already reached the maximum retry count, **when** evaluated, **then** the decision is always no-retry, regardless of reason.

### US-4.4 — Reconnect and resume
*As a Contact who gets reconnected, I want the agent to acknowledge the drop and pick up where we left off, so that the call feels continuous.*
**Refs:** FR-4.5, FR-4.6, FR-4.7

- **Given** a positive retry decision, **when** the system redials, **then** the previously saved conversation memory is reloaded before the conversation resumes.
- **Given** the reconnected call is answered, **when** the agent speaks first, **then** its opening line acknowledges the earlier disconnect (e.g. apologizes and references the interruption).
- **Given** the conversation resumes, **then** it rejoins the same listen → understand → respond loop from Epic 2 — it does not restart as a new, context-free call.
- **Given** the reconnect attempt itself fails to connect, **when** this happens, **then** it is handled as a new disconnect/retry evaluation, not silently dropped.

### US-4.5 — Close out a non-retried call
*As a Campaign Manager, I want a call that isn't worth retrying to be closed out properly, so that it still produces usable data.*
**Refs:** FR-4.8, FR-4.9, FR-4.10

- **Given** a negative retry decision, **when** applied, **then** the call's status is set to `Completed (Partial)`.
- **Given** that status, **then** post-call analysis still runs on the partial transcript, producing a lead score and interest classification as it would for a normal call.
- **Given** the same call, **then** a disposition is recorded (e.g. "Customer Hung Up", "Technical Failure — No Retry Left").
- **Given** a call is marked `Completed (Partial)`, **when** any later process checks it, **then** no further retry is attempted against it.

---

## Epic 5 — Final output

### US-5.1 — One output record per contact
*As a Sales team member, I want one complete output record per contact regardless of how the call ended, so that I never have to check multiple places for the result.*
**Refs:** FR-5.1, FR-5.2, FR-5.3

- **Given** a call reaches any terminal state — normal completion, recovered completion, or partial completion — **when** processing finishes, **then** exactly one final output record exists for that contact attempt.
- **Given** the record's source, **then** it originates from either the Epic 3 normal-completion path or the Epic 4 partial-completion path — never from an incomplete or in-progress call.
- **Given** the record is generated, **then** it includes: full transcript, recording, feedback, interest detection, lead score, call summary, and an admin contact view.
- **Given** a contact has multiple attempts before reaching a terminal state, **when** the final record is generated, **then** it reflects the terminal attempt while still linking to the full attempt history.

### US-5.2 — Full attempt history view
*As an Admin, I want to see the full attempt history and timeline for a contact, so that I understand how their outcome was reached.*
**Refs:** FR-5.3 (contact view)

- **Given** a contact with multiple call attempts, **when** I open their admin view, **then** every attempt is listed in chronological order.
- **Given** an attempt in the timeline, **when** selected, **then** its recording, transcript, and analysis are accessible from that entry.

---

## Epic 6 — Retry policy configuration

### US-6.1 — Retry limits and spacing
*As an Admin, I want to set a maximum retry count and retry spacing, so that contacts aren't over-called.*
**Refs:** FR-6.1, FR-6.2

- **Given** a configured max-retry value (default: 2), **when** a contact reaches that many failed/disconnected attempts, **then** no further retries are scheduled.
- **Given** configured retry spacing (default: Retry #1 at 30 sec → Retry #2 at 10 min), **when** a retry is scheduled, **then** it is not dispatched before the corresponding interval has elapsed.
- **Given** both never-connected retries (Epic 1) and mid-call-disconnect retries (Epic 4), **then** both respect the same max-retry and spacing configuration, evaluated against their own reason taxonomy (`never_connected_rules` vs `mid_call_rules` — Call State Machine §5.1–§5.2).

### US-6.2 — Reason-based retry rules
*As an Admin, I want to control which failure types are retried, so that rejections and other non-retryable reasons are respected and not retried.*
**Refs:** FR-6.3, FR-6.4, FR-6.5

- **Given** the `never_connected_rules` policy has `no_answer`, `busy`, `network_error`, or `provider_error` enabled (all default true), **when** the corresponding never-connected failure occurs, **then** a retry is scheduled (subject to max-retry count).
- **Given** the `never_connected_rules` policy has `invalid_number` or `rejected` disabled (default), **when** either occurs, **then** no retry is scheduled, regardless of remaining retry budget.
- **Given** the `mid_call_rules` policy has `technical_issue`, `network_problem`, `provider_error`, `ai_error`, or `unknown` enabled (all default true), **when** the corresponding mid-call disconnect occurs, **then** a retry is scheduled (subject to max-retry count).
- **Given** the `mid_call_rules` policy has `customer_hangup` disabled (default), **when** a customer-hangup disconnect occurs, **then** no retry is scheduled by default — evaluated through the same policy engine as any other reason, with no hardcoded bypass.

### US-6.3 — Calling window enforcement
*As an Admin, I want to restrict calling to a defined time window, so that contacts aren't called outside acceptable hours.*
**Refs:** FR-6.6

- **Given** a configured calling window (default: 10 AM – 6 PM), **when** a call or retry is due outside that window, **then** it is deferred until the window next opens.
- **Given** a call is already in progress when the window closes, **when** this happens, **then** the in-progress call is allowed to complete — the window only gates new dial attempts.

---

*No lost conversations. More opportunities. Higher conversion.*
