# AI Calling Agent — AI Agent Specification

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** PRD v1.0 · FRS v1.0 · System Architecture v1.0 · Call State Machine v1.0 · Detailed Workflow v1.0

**Purpose:** the System Architecture document treats the Language Model as a component; this document specifies what that component must actually do — the agent's persona, conversation logic, memory, guardrails, and behavior on reconnect. It is the content/behavior layer inside the Conversation Orchestrator (`FR-2.1`–`FR-2.8`).

---

## 1. Agent persona

| Attribute | Specification |
|---|---|
| Role | Represents the calling campaign's brand — introduces itself honestly as an automated calling assistant unless the campaign configuration specifies otherwise |
| Tone | Natural, warm, concise — matches the campaign's configured tone (e.g. formal for B2B outreach, casual for consumer outreach) |
| Pace | Speaks at a natural conversational pace; avoids long unbroken monologues — breaks information into turns the contact can respond to |
| Honesty | Does not claim to be human if directly asked; does not fabricate information it doesn't have |

---

## 2. Conversation objective model

Each campaign configures the agent with:

- **Goal(s):** the outcome the call is working toward (e.g. qualify interest, book a follow-up, confirm details).
- **Script skeleton:** an ordered set of talking points/questions the agent should cover, not a rigid verbatim script — the agent adapts phrasing and order to the flow of the conversation while still covering required points.
- **Required fields:** specific pieces of information the agent must attempt to capture during the call (e.g. budget range, timeline, decision-maker status) — these map to entity extraction (§4).
- **Exit conditions:** what marks the call as reaching a natural end (goal achieved, contact declines, script exhausted).

`[FR-2.4]`

---

## 3. Turn-by-turn decision logic

For every completed utterance from the contact, the agent performs, in order:

1. **Transcription check** — confirm the utterance is complete (silence detected) before acting. `[FR-2.1, FR-2.2]`
2. **Intent classification** — map the utterance to one of the intent categories in §5.
3. **Entity extraction** — pull out any required-field values present in the utterance (§4).
4. **Memory update** — merge new intent/entity information into the conversation memory (§6); later turns must be able to reference anything captured here.
5. **Next-action decision** — using memory + script skeleton + goal, decide one of: continue script, answer a question, handle an objection, confirm and move to close, or end the call.
6. **Response generation** — produce a natural-language reply consistent with persona and tone.
7. **Speech rendering** — convert to speech; remain interruptible while speaking. `[FR-2.5, FR-2.6]`

`[FR-2.3, FR-2.4]`

---

## 4. Entity extraction

Entities are campaign-configurable, but the agent must support at minimum:

| Entity | Example | Notes |
|---|---|---|
| Interest level | Interested / not interested / undecided | Feeds directly into post-call lead scoring (`FR-3.2`) |
| Objection | Price, timing, trust, not the right contact | Logged even if not resolved in-call |
| Follow-up preference | Callback time, preferred channel | Captured verbatim where possible, not paraphrased |
| Decision-maker status | Sole decision-maker / influencer / not involved | Used for lead qualification |
| Free-text feedback | Any unprompted comment relevant to the campaign | Stored for the Feedback output field |

Extraction failures (an entity the agent could not confidently determine) are recorded as `not captured` rather than guessed.

---

## 5. Intent taxonomy

Minimum intent categories the agent must be able to classify an utterance into:

- `affirmative` — agreement, yes, positive interest
- `negative` — disagreement, no, decline
- `question` — the contact is asking the agent something
- `objection` — a stated reason for hesitation
- `request_callback` — asks to be contacted later instead
- `request_human` — asks to speak with a person
- `off_topic` — unrelated to the call's purpose
- `end_call` — indicates they want to end the call now
- `unclear` — could not be confidently classified

`request_human` and repeated `unclear` classifications are the two intents most likely to require escalation handling (§8).

---

## 6. Conversation memory schema

Memory accumulates for the life of a call attempt and is what gets saved on disconnect (`FR-4.1`) and reloaded on reconnect (`FR-4.5`).

| Field | Description |
|---|---|
| `turns` | Ordered list of (utterance, intent, response) for the call so far |
| `captured_entities` | Current values for all entities defined in §4, updated as they're captured |
| `script_progress` | Which script skeleton points have been covered vs. still pending |
| `objections_raised` | List of objections raised and whether addressed |
| `last_agent_utterance` | The agent's most recent line, used to construct a coherent resume greeting on reconnect |

**Note:** `recording_consent` is tracked separately from this table, as a top-level structured-output field (Prompt Specification §4) rather than an entity in §4 above — see Recording-Consent.md §4 for its capture and persistence rules.

**Reconnect rule:** on resume, the agent must reference `last_agent_utterance` and/or `script_progress` in its opening line so the contact perceives continuity, not a restart. `[FR-4.6, FR-4.7]`

---

## 7. Interruption handling

- If the contact begins speaking while the agent is mid-response, the agent stops speaking within a short, bounded delay and processes the new input as the next turn. `[FR-2.6]`
- The agent does not resume its interrupted sentence verbatim afterward — it re-evaluates the next action given the new utterance, since continuing an interrupted point may now be redundant or contradicted.
- Brief acknowledgements from the contact ("mm-hmm", "okay") while the agent is speaking are not treated as interruptions requiring a full stop.

---

## 8. Guardrails

- **No fabrication:** the agent must not invent facts (pricing, availability, policy details) it has not been configured with; it should offer to follow up or connect the contact with a human resource instead.
- **Respect decline signals:** an `intent = negative` combined with a clear decline to continue should move the agent toward a polite close, not repeated persistence past a second decline.
- **`request_human` handling:** since live agent hand-off is out of scope for this system (per the PRD), the agent should acknowledge the request, set `next_action = escalate_to_human_offer` (Prompt Specification §4) to offer a callback or alternative contact method per campaign configuration, and then proceed to a polite close (`next_action = end_call_polite`).
- **Compliance boundaries:** the agent must not proceed past a contact's request to be removed from the calling list — this should be captured as a required field and propagated back to suppress future retries and future campaigns, not just the current call.
- **Repeated `unclear` intents:** if an utterance cannot be classified after a reasonable number of clarification attempts, the agent should gracefully end the call rather than loop indefinitely.

---

## 9. Reconnect behavior (detail)

Expanding on Detailed Workflow Step 4.5:

1. Load `conversation memory` for the attempt being resumed.
2. Construct an opening line that: (a) acknowledges the disconnect, (b) briefly references where the conversation left off using `last_agent_utterance` or `script_progress`, (c) invites the contact to continue.
3. Resume the turn-by-turn loop (§3) from that point — `script_progress` determines what's still outstanding, so the agent does not re-ask already-captured entities unless clarifying a possibly-stale answer.

Example opening line pattern (illustrative, not verbatim script):
> "Sorry about that, we got disconnected — you were just telling me about [last topic]. Would you like to pick up from there?"

---

## 10. Configuration surface

Fields a campaign manager can set for the agent, exposed via the admin dashboard (`FR-3.6` covers retry policy; the following are the agent's own configuration, set at campaign creation):

| Setting | Purpose |
|---|---|
| Persona/tone | Formal vs. casual, brand voice |
| Script skeleton | Ordered talking points/questions |
| Required entity fields | What must be attempted to capture |
| Goal definition | What "success" means for this campaign |
| Exit phrases | Campaign-specific phrases that should end the call gracefully |
| Escalation contact method | What to offer when `request_human` occurs |

---

## 11. Evaluation criteria

Post-call analysis (`FR-3.1`, `FR-3.2`) should be able to assess, using the memory schema above:

- Was the goal achieved (fully / partially / not at all)?
- Were all required entity fields captured?
- Was the contact interrupted or talked over more than expected?
- Did any guardrail trigger during the call (decline signal, `request_human`, repeated `unclear`)?
- On a recovered call, did the resumed portion stay coherent with the pre-disconnect portion?

These map onto the Call Summary and Feedback fields in the Final Output record (`FR-5.3`).

---

*No lost conversations. More opportunities. Higher conversion.*
