# AI Calling Agent — Recording Consent

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Data Privacy v1.0 · Conversation Flow v1.0 · AI Agent Specification v1.0 · Database Design v1.0 · Security Architecture v1.0

> **Note:** as with Data Privacy, this document specifies product behavior and data handling, not legal advice. Whether a jurisdiction requires one-party or all-party consent to record a call — and exactly what disclosure satisfies it — is a legal determination that varies by region and should be confirmed with counsel. This document assumes that determination has been made per campaign/jurisdiction and specifies how the system enforces it.

---

## 1. Purpose

Call recording (Database Design §2.9, `recording_ref`) touches a specific compliance requirement distinct from general data privacy: in many jurisdictions, recording a call requires the recorded party's knowledge or explicit consent, obtained *before* recording begins. This document specifies where that disclosure happens in the conversation, how consent (or objection) is captured and logged, and what the system does in either case.

---

## 2. Consent models

Two general models exist across jurisdictions (described generally, not as legal guidance):

| Model | Description |
|---|---|
| **One-party consent** | Recording is permitted as long as one party (here, the calling organization) is aware and has agreed — the contact's awareness is not strictly required, though disclosure is still often best practice |
| **All-party consent** | Every party on the call must be informed (and, depending on jurisdiction, must not object) before recording is permitted |

**System stance:** because a single campaign can call contacts across multiple jurisdictions, the system defaults to the more conservative behavior — **disclose and allow objection** — unless a campaign is explicitly configured otherwise for a jurisdiction confirmed to require only one-party consent.

---

## 3. Where disclosure happens

Disclosure is placed at the very start of the **Opening** phase (Conversation Flow §3.1), before any substantive conversation content, and — critically — before recording of the substantive conversation begins (see §5 on the pre-disclosure recording boundary).

**Recommended default disclosure line** (configurable per campaign, per §7):
> "Hi, this is [agent] calling on behalf of [brand]. This call may be recorded for quality and follow-up purposes — is that okay with you?"

This replaces the plain greeting used in Conversation Flow §3.1 when recording-consent disclosure is enabled for the campaign's configured jurisdiction.

---

## 4. Consent capture

The contact's response to the disclosure line is classified using the same intent taxonomy as the rest of the conversation (Prompt Specification §4), with recording consent treated as its own captured entity, distinct from `interest_level`:

| Response | Captured value |
|---|---|
| Affirmative / no objection | `recording_consent = granted` |
| Explicit objection ("no", "I'd rather you didn't") | `recording_consent = denied` |
| Unclear / no clear response after one clarification | `recording_consent = unclear` |

This value is produced as the structured output's top-level `recording_consent` field (Prompt Specification §4 — not nested under `entities`/`captured_entities`, since it is a compliance capture rather than a lead-scoring entity), written to working memory as its own `recording_consent` field (Memory Specification §2), and persisted to the `call_attempt.recording_consent` column (Database Design §2.5) at Finalized, so it is retrievable independently of the transcript.

---

## 5. The pre-disclosure recording boundary

- Recording of the substantive conversation must not begin until the disclosure line has been delivered and a response classified.
- The disclosure line itself, and the contact's response to it, **may** be recorded/logged as a short, separate artifact even under an all-party-consent posture, since disclosure-and-response is generally the mechanism by which consent is established, not content requiring its own prior consent. Where this is not legally sound for a given jurisdiction, campaign configuration should disable even this minimal capture (§7).
- If recording has not started until disclosure completes, the system does not lose conversational content in the meantime — the transcript (Database Design §2.8) is a separate artifact from the recording and may still capture the exchange via speech-to-text even while audio recording is deferred, if campaign configuration permits transcript-without-audio in this narrow window.

---

## 6. Handling an objection or unclear response

| Outcome | System behavior |
|---|---|
| `recording_consent = denied` | Audio recording is not started (or is stopped/discarded if any pre-disclosure buffer existed). The call **continues** — denial of recording consent is not treated as ending the call — using transcript-only capture if the jurisdiction/campaign configuration permits continuing without an audio recording; if configuration requires an audio recording for the call to proceed (e.g. for dispute-resolution reasons), the agent explains this and offers to end the call or proceed without objection to a compromise (e.g. transcript-only, campaign-configurable). |
| `recording_consent = unclear` | The agent asks once more for a clear yes/no before proceeding to any recorded portion of the call. If still unclear, treat as `denied` for safety, not as `granted`. |
| `recording_consent = granted` | Recording proceeds normally for the remainder of the call. |

**Guardrail:** the system must never default an unclear or absent consent response to `granted`. Absence of a clear answer is treated as the more conservative outcome (§2's conservative-default principle).

---

## 7. Jurisdiction-based configuration

- Campaigns should be configurable to associate a consent model (§2) with the calling region — e.g. derived from the contact's phone number area code/country code, or set at the campaign level if all contacts share a jurisdiction.
- Configuration surface (extending AI Agent Specification §10):

| Setting | Purpose |
|---|---|
| `recording_disclosure_enabled` | Whether the disclosure line (§3) is used at all |
| `recording_disclosure_text` | Campaign-specific wording, if the default (§3) needs adaptation |
| `require_explicit_consent` | If true, an `unclear` or `denied` response blocks audio recording entirely (all-party-consent posture); if false, disclosure is given but the call may proceed to recording on a lack of objection (one-party posture) |
| `allow_transcript_without_recording` | Whether the call can continue with transcript-only capture when recording is denied |

---

## 8. Retention tied to consent

- A recording captured under `recording_consent = granted` follows the standard retention rules in Data Privacy §2 / Security Architecture §7.
- If a recording exists from before a `denied` response was fully processed (a race or implementation edge case), that recording must be deleted, not merely marked unused — retaining an unconsented recording is itself the compliance failure being guarded against, regardless of whether it's later accessed.
- The `recording_consent` value itself is retained alongside the call attempt record for as long as the attempt's other data is retained, since it may be needed to demonstrate compliance.

---

## 9. Edge cases

| Scenario | Handling |
|---|---|
| Contact disconnects during or immediately after the disclosure line, before responding | `recording_consent = unclear`; no recording exists to worry about retaining, since none had started |
| Reconnect after a disconnect that occurred *after* consent was already granted | Consent does not need to be re-asked on reconnect — `recording_consent` is part of working memory (§4) and is reloaded with the rest of the conversation memory (Memory Specification §3, "Reloaded") |
| Reconnect after a disconnect that occurred *during* the disclosure exchange itself (consent not yet resolved) | Disclosure must be completed and a clear response obtained before recording resumes, exactly as on the original attempt — the reconnect does not skip this step |
| Campaign spans multiple jurisdictions with different consent models | Configuration should be set at the most conservative common denominator unless per-contact jurisdiction detection (§7) is in place to vary it correctly per call |

---

## 10. Traceability

| Element | Reference |
|---|---|
| Disclosure placement | Conversation Flow §3.1 (Opening) |
| Consent as a captured entity | Memory Specification §2, Prompt Specification §4 |
| Recording artifact | Database Design §2.9 (`recording_ref`) |
| Retention & deletion of unconsented recordings | Data Privacy §2, Security Architecture §7 |
| Conservative-default principle | Data Privacy §10 (Privacy by design) |

---

*No lost conversations. More opportunities. Higher conversion.*
