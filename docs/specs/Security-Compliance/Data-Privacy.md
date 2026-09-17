# AI Calling Agent — Data Privacy

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Security Architecture v1.0 · Memory Specification v1.0 · Database Design v1.0 · AI Agent Specification v1.0 · Lead Scoring Specification v1.0

> **Note:** this document specifies the product's privacy-relevant behavior and data handling for engineering and product purposes. It is not legal advice. Applicable obligations (e.g. TCPA, GDPR, CCPA, or other regional telemarketing and data-protection law) vary by jurisdiction and should be confirmed with legal counsel before a campaign goes live in a given region.

---

## 1. Purpose & scope

Where Security Architecture defines *technical* controls (encryption, access control, threat model), this document defines what personal data the system handles, why, for how long, and what rights and disclosures apply to the people it calls.

---

## 2. Personal data inventory

| Data | Source | Purpose | Retention | System reference |
|---|---|---|---|---|
| Phone number | Contact import | Required to place the call | Per configured retention window; permanent in `suppression` if opted out | Database Design §2.4, §2.12 |
| Call recording | Telephony provider, during the call | Quality, analysis, dispute resolution | Configurable retention window | Database Design §2.9 |
| Transcript | Speech-to-text, during the call | Conversation processing, analysis, record-keeping | Configurable retention window | Database Design §2.8 |
| Captured entities (interest, objections, decision-maker status, free-text feedback) | Conversation, extracted by the language model | Lead qualification, follow-up | Configurable retention window; `cumulative_entities` may persist longer in contact history for legitimate follow-up purposes | Memory Specification §2, §7 |
| Lead score, qualification, disposition | Post-call analysis | Sales prioritization | Tied to Final Output retention | Lead Scoring Specification |
| Suppression / do-not-call record | Explicit contact request | Compliance — prevents future contact | Indefinite, exempt from standard expiry | Security Architecture §7 |

**Data minimization:** the system should only request/require entity fields that are actually relevant to the campaign's stated goal (AI Agent Specification §2, `required_entity_fields`) — campaigns should not be configured to capture entities unrelated to their purpose.

---

## 3. Legal basis & consent

- Outbound calling campaigns typically rely on an existing business relationship, prior consent, or another applicable legal basis, depending on jurisdiction and the nature of the campaign (e.g. B2B vs. consumer). This determination is a legal/compliance decision made per campaign, outside this system's scope to adjudicate — the system's role is to **enforce** the outcome of that decision (e.g. via the calling window and suppression list), not to determine legality itself.
- Where consent is the basis for calling, the campaign manager is responsible for ensuring the imported contact list reflects only contacts for whom that consent exists before import (Detailed Workflow, Step 1.1).

---

## 4. Automated-call disclosure

Per AI Agent Specification §1 ("Honesty"), the agent must not claim to be human if directly asked, and should identify itself as an automated assistant unless campaign configuration specifies otherwise for a permitted use case. This behavior should default to **on** (disclose) for every new campaign; disabling it should require an explicit, logged configuration choice by an Admin, since many jurisdictions require disclosure that a call is automated at or near the start of the call.

**Recommended default opening line pattern** (Conversation Flow §3.1): the agent identifies itself as calling "on behalf of [brand]" and, per campaign configuration, may explicitly state it is an automated assistant.

---

## 5. Data subject rights

| Right | System capability | Reference |
|---|---|---|
| **Opt-out / do-not-call** | `POST /contacts/{contactId}/suppress` records a permanent suppression, checked on every dial including retries, and matched by phone number across future campaigns | API Specification, Security Architecture §7–§8 |
| **Access** | `GET /contacts/{contactId}` and `GET /contacts/{contactId}/final-output` expose everything held about a contact | API Specification |
| **Erasure** | A full erasure request cascades across contact, attempts, transcripts, recordings, analysis, contact history, and final output, while a minimal suppression record is retained by phone number to prevent re-import | Security Architecture §7 |
| **Correction** | Not currently modeled as a distinct endpoint; a correction request should be handled as a data-management action outside the automated pipeline (e.g. updating `contact.phone_number` or removing an incorrectly captured entity) | — (gap noted in §9) |
| **Portability** | The Final Output record (`FR-5.3`) is already structured (JSON) and can serve as a portable export of what the system holds on a contact | API Specification `FinalOutput` schema |

---

## 6. Suppression enforcement detail

Suppression is the single most safety-critical privacy mechanism in this system, since a failure here directly causes unwanted contact:

- Checked at initial queue entry **and** at every scheduled retry re-entry (Security Architecture §8) — a retry scheduled before a suppression request must not bypass a suppression added afterward.
- Keyed by phone number, not only `contact_id`, so a suppressed number re-imported under a new contact record in a future campaign is still caught (Database Design §2.12).
- Set by the agent itself in-call (`requires_suppression = true`, Prompt Specification §4) as well as by manual API call — both paths write to the same `suppression` table (distinguished by `source = agent_in_call` vs `manual_api`, Database Design §2.12), so neither path can be "missed" by the other's absence.

---

## 7. Third-party data processors

The system shares personal data with external services as part of normal operation. Each should be covered by an appropriate data processing agreement before use in a live campaign:

| Processor | Data shared | Purpose |
|---|---|---|
| Telephony provider | Phone number, call audio | Placing and connecting the call |
| Speech-to-text service | Call audio | Real-time transcription |
| Language model provider | Transcribed conversation content, campaign script/goals | Intent understanding and response generation |
| Text-to-speech service | Generated response text | Speech rendering |

**Minimization at the provider boundary:** only the data each provider needs for its specific function should be sent — e.g. the LLM provider receives conversation content and campaign configuration, not raw recordings or unrelated contact fields it doesn't use.

---

## 8. Cross-border data transfer

If any of the processors in §7 operate in a different jurisdiction than the contact being called, cross-border transfer requirements (e.g. standard contractual clauses, adequacy decisions) may apply. This is a per-deployment, per-jurisdiction determination outside this document's scope, but the Infrastructure Architecture's regional placement decisions (§5, "Placement implication") should be made with this in mind — not purely on latency grounds.

---

## 9. Known gaps / open items

- **Correction workflow:** no dedicated endpoint currently exists for a contact to request correction of inaccurate captured data (as opposed to full erasure). This should be added if required by applicable regulation.
- **Consent record-keeping:** the system enforces suppression once requested, but does not itself store evidence of the original consent/legal basis for a campaign — that is expected to be managed by the campaign manager outside this system, and may need to be brought in-scope depending on jurisdictional record-keeping requirements. This is a deliberate, documented scope boundary, not an oversight: `suppression` (Database Design §2.12) is the system's single canonical DNC/opt-out record, and no other document in this set should introduce a separate `consents` or `dnc_records` table without first updating this section.

---

## 10. Privacy by design summary

Principles already reflected elsewhere in the documentation set, restated here for a privacy-focused reading:

- Data collected is limited to what the campaign's configured goal requires (§2, AI Agent Specification §2).
- Suppression, once requested, cannot be silently bypassed by any path in the system (§6).
- Retention is time-bound by default, with suppression as the explicit, documented exception (Security Architecture §7).
- Encryption and access control apply to all personal data by default, not as an opt-in (Security Architecture §1, §4).
- The automated nature of the call is disclosed by default, not hidden by default (§4).

---

## 11. Traceability

| Privacy element | Reference |
|---|---|
| Suppression mechanics | Security Architecture §7–§8, Database Design §2.12 |
| Retention & erasure | Security Architecture §7, Memory Specification §9 |
| Disclosure behavior | AI Agent Specification §1 |
| Data captured | Memory Specification §2, §4 (entity extraction) |
| Third-party sharing | System Architecture §7 (External integrations) |

---

*No lost conversations. More opportunities. Higher conversion.*
