# AI Calling Agent — Test Plan

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** FRS v1.0 · Acceptance Criteria v1.0 · Call State Machine v1.0 · Webhook Specification v1.0 · Threat Model v1.0 · Lead Scoring Specification v1.0 · Infrastructure Architecture v1.0

**Purpose:** define what gets tested, at what level, with what data, and what must pass before release. This plan converts the Acceptance Criteria into an execution strategy and explicitly closes out the open test gaps identified in the Threat Model's risk register (§6 there).

---

## 1. Test levels

| Level | Scope | Primary source |
|---|---|---|
| Unit | Individual functions/components in isolation (entity extraction parsing, retry-spacing calculation, score-band lookup) | FRS, Lead Scoring Specification |
| Integration | Component-to-component behavior (Orchestrator ↔ State Saver, Retry Policy Engine ↔ Calling Queue) | System Architecture, Call State Machine |
| End-to-end (E2E) | Full contact journeys through the actual pipeline | Acceptance Criteria, Detailed Workflow's "End-to-end path summary" |
| Load / chaos | Behavior under volume and induced failure | Infrastructure Architecture §6, Threat Model §5.2 |
| Security | Authn/authz, injection resistance, signature verification | Security Architecture, Threat Model |
| Regression | Re-run of previously found defects and risk-register items | Threat Model §6 |

---

## 2. Functional test suite (from Acceptance Criteria)

Every Given/When/Then in Acceptance Criteria v1.0 becomes one or more test cases, organized by epic:

| Epic | Test focus | Example cases |
|---|---|---|
| Epic 1 — Calling system | Import validation, dedup, queue scheduling, failure classification, retry-to-queue (not import) | US-1.1–US-1.5 ACs |
| Epic 2 — Conversation engine | Turn loop correctness, interruption handling, context continuity | US-2.1–US-2.4 ACs |
| Epic 3 — Data & admin | Analysis output completeness, persistence, dashboard accuracy | US-3.1–US-3.4 ACs |
| Epic 4 — Disconnect & recovery | State preservation, reason classification, retry evaluation, reconnect continuity, partial close-out | US-4.1–US-4.5 ACs |
| Epic 5 — Final output | Single-record guarantee, dual-source generation (normal + partial) | US-5.1–US-5.2 ACs |
| Epic 6 — Retry policy | Limits, spacing, reason-based rules, calling window | US-6.1–US-6.3 ACs |

**Exit criteria for this suite:** 100% of Acceptance Criteria Given/When/Then statements have a passing automated test, with no open defects rated High or Critical.

---

## 3. State machine test suite

Derived directly from Call State Machine v1.0's transition tables.

- **Every legal transition** in §2.2 (Contact) and §3.2 (Call Attempt) has at least one test that exercises it.
- **Every illegal transition** listed in Call State Machine §6 has a negative test confirming the system rejects or cannot produce it — specifically:
  - A never-connected retry cannot re-enter `Dialing` bypassing `Pending`, and cannot route to the contact-import step.
  - A `FailedToConnect` attempt cannot resolve to `CompletedPartial`.
- **Guard boundary tests:** exact-boundary cases for `attempts < max_retries` (e.g. at exactly max_retries − 1 and at max_retries), and calling-window edges (a call queued one second before window close).

---

## 4. Disconnect & recovery race-condition test (closes Threat Model risk #4)

Specifically targets the ordering invariant: *the disconnect snapshot must be durably committed before any retry decision is evaluated.*

- **Test design:** inject an artificial delay in the snapshot write path and simultaneously trigger a retry-decision evaluation; assert the decision is blocked/queued until the snapshot commit is acknowledged, never evaluated against a not-yet-durable state.
- **Test design (failure injection):** simulate a snapshot write failure and confirm the retry decision does not proceed at all — it must not "fail open" into an unretried or, worse, an incorrectly retried state.
- **Exit criteria:** test passes under repeated randomized timing (property-based / fuzz-style execution across many delay values), not just a single fixed-delay case.

---

## 5. Suppression bypass regression test (closes Threat Model risk #1)

- **Test design:** suppress a contact mid-campaign, then force a previously-scheduled retry (queued before suppression) to fire; assert the dial does not occur.
- **Test design:** suppress a phone number, then re-import the same number under a new `contact_id` in a different campaign; assert the new contact is not dialed.
- **Test design:** trigger suppression via the agent's in-call `requires_suppression = true` path and separately via the manual API endpoint; assert both write to the same `suppression` table and both are honored identically.
- **Exit criteria:** these three cases run on every deployment as a release gate, not only in a periodic regression pass — per Threat Model's recommendation that this is a "must never happen" class of failure.

---

## 6. Webhook test suite

From Webhook Specification v1.0:

- Signature verification: valid signature accepted; invalid/missing signature rejected with `401` and logged.
- Replay protection: a valid, correctly-signed but stale (>5 min old) event is rejected.
- Idempotency: the same `event_id` delivered twice produces exactly one state change, not two.
- Ordering/race handling: a `call.disconnected` arriving after `call.ended` for the same attempt is a logged no-op, not a state corruption.
- Durable-before-ack: kill the process after webhook receipt but before internal persistence completes; confirm the event is not lost (either not yet acknowledged, or durably enqueued before the `200`).

---

## 7. Security test suite

| Area | Test focus | Reference |
|---|---|---|
| Authentication | Expired/invalid JWT rejected on all bearer-authenticated endpoints | Security Architecture §2 |
| Authorization (RBAC) — closes Threat Model risk #5 | Explicit test matrix: every role × every endpoint, asserting allowed/denied as specified in Security Architecture §3. Specifically confirm a Campaign Manager token cannot write `retry_policy` or `agent_config`. | Security Architecture §3 |
| Rate limiting — closes Threat Model risk #6 | Confirm documented concrete thresholds (once defined) are enforced; confirm legitimate burst traffic under the threshold is not throttled | Threat Model §4.1, §4.2 |
| Encryption | Confirm PII fields are encrypted at rest by inspecting raw storage; confirm field-level encryption coverage matches Security Architecture §4's list — closes Threat Model risk #7 | Security Architecture §4 |
| Prompt injection resistance | Adversarial test set of crafted contact utterances attempting to: extract the system prompt, override persona/guardrails, elicit unauthorized commitments (pricing, legal promises) | Threat Model §5.3 |
| Output-side guardrail validation — closes Threat Model risk #3 | Once implemented, test that `response_text` containing an unauthorized commitment is flagged/blocked even if the model produced it despite prompt-side guardrails | Threat Model §5.3 |

---

## 8. Recording consent test suite

From Recording Consent v1.0:

- Disclosure line is delivered before any audio recording begins (verify no audio artifact exists prior to consent classification).
- `granted` → recording proceeds; `denied` → no recording exists, call continues per configuration; `unclear` → one re-prompt, then treated as `denied` if still unclear.
- A recording created before a `denied` classification is fully processed (simulated race) is confirmed deleted, not just unflagged.
- Reconnect after consent was already granted does not re-prompt; reconnect during an unresolved disclosure exchange re-completes disclosure before recording resumes.

---

## 9. Lead scoring validation suite

From Lead Scoring Specification v1.0:

- Component weighting sums correctly to the total score across representative signal combinations (including the worked example in §3).
- Band thresholds (Hot/Warm/Cold, qualification levels) produce the correct tag at each boundary value.
- Partial-call scoring: missing fields contribute zero, never an estimated/guessed value; `score_confidence = partial` is set correctly.
- Suppression override: a suppressed contact's qualification level is forced to `Unqualified` regardless of numeric score.

---

## 10. Load and chaos testing (closes Threat Model risk #2)

- **Scale test:** sustain dispatch and processing for a 100,000+ contact campaign at the configured call rate without queue backlog growth exceeding an agreed threshold.
- **Concurrent-call test:** scale Conversation Orchestrator pods under a target concurrent-call count and confirm per-turn latency stays within budget (Infrastructure Architecture §5).
- **Outage-shaped webhook flood:** specifically simulate a telephony provider outage producing a burst of legitimate `call.disconnected` events at a rate similar to an attack pattern; confirm rate limiting does not drop or excessively delay legitimate events. This is the specific gap flagged in Threat Model §5.2 and must be closed before this risk can be marked fully mitigated.
- **Retry burst test:** many contacts becoming eligible for a scheduled retry at the same instant (e.g. a wave of 30-second retries); confirm fresh dials are not starved.

---

## 11. Test data management

- Synthetic contact data only in non-production environments — no real contact PII in Development or Staging.
- A dedicated set of adversarial transcripts/utterances maintained for the prompt-injection suite (§7), version-controlled and expanded whenever a new injection pattern is identified.
- Sandboxed telephony/STT/LLM/TTS provider credentials used in Staging, per Infrastructure Architecture §9.

---

## 12. Defect management

- Defects are triaged against the same Risk (Likelihood × Impact) rating used in the Threat Model, for consistency between security findings and general defects.
- Any defect touching suppression, the disconnect-save ordering invariant, or recording consent is treated as release-blocking regardless of severity classification elsewhere, given their compliance/data-loss implications.

---

## 13. Entry / exit criteria for release

**Entry (start of test cycle):** FRS and Acceptance Criteria for the release scope are finalized; test environment matches Staging per Infrastructure Architecture §9.

**Exit (release gate):**
- All Acceptance Criteria test cases pass.
- All Call State Machine legal/illegal transition tests pass.
- Suppression regression suite (§5) passes.
- Disconnect race-condition suite (§4) passes under randomized timing.
- Webhook signature/idempotency/ordering suite (§6) passes.
- RBAC authorization matrix (§7) fully executed with no unauthorized-access findings.
- No open High/Critical defects; no open Threat Model risk-register item without either a passing closing test or an explicitly accepted residual risk sign-off.

---

## 14. Traceability

| Test area | Source document |
|---|---|
| Functional suite | Acceptance Criteria v1.0 |
| State machine suite | Call State Machine v1.0 |
| Webhook suite | Webhook Specification v1.0 |
| Security suite | Security Architecture v1.0, Threat Model v1.0 |
| Recording consent suite | Recording Consent v1.0 |
| Lead scoring suite | Lead Scoring Specification v1.0 |
| Load/chaos suite | Infrastructure Architecture v1.0, Threat Model §5.2 |

---

*No lost conversations. More opportunities. Higher conversion.*
