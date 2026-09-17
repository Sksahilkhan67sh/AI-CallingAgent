# AI Calling Agent — Lead Scoring Specification

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** PRD v1.0 · FRS v1.0 · AI Agent Specification v1.0 · Memory Specification v1.0 · Conversation Flow v1.0

**Purpose:** the Lead Score, qualification level, hot/warm/cold tag, and conversion probability appear in the Final Output record (`FR-5.3`) and are produced by Post-Call Analysis (`FR-3.1`, `FR-3.2`). This document specifies exactly what feeds that score, how it's computed, and how it behaves for partial (disconnected, non-recovered) calls.

---

## 1. Output fields covered

| Field | Type | Range / values |
|---|---|---|
| Lead score | Integer | 0–100 |
| Qualification level | Enum | `Unqualified`, `Marginal`, `Qualified`, `Highly Qualified` |
| Temperature tag | Enum | `Hot`, `Warm`, `Cold` |
| Conversion probability | Percentage | 0–100% |

All four are derived from the same underlying signals (§2) but serve different consumers — the temperature tag for quick triage, qualification level for filtering, lead score for ranking, and conversion probability for forecasting.

---

## 2. Scoring inputs

Pulled from working memory (Memory Specification §2) and the finalized transcript at the point Post-Call Analysis runs.

| Signal | Source | Contribution |
|---|---|---|
| `interest_level` entity | Memory Specification §2 | Primary driver — interested / undecided / not_interested |
| `decision_maker_status` entity | Memory Specification §2 | Qualification weight — sole decision-maker scores higher than influencer or not-involved |
| Objections raised vs. addressed | `objections_raised` | Unaddressed objections reduce score; addressed objections have a smaller penalty |
| Script completion | `script_progress` | How much of Discovery was actually completed — a call that ended early has less signal to score on |
| Engagement quality | `turns` (count, length, interruption pattern) | Longer, substantive exchanges score higher than short, deflecting answers |
| Explicit next step | `next_action` history reaching `confirm_and_close` / `end_call_goal_met` | A confirmed next step is a strong positive signal |
| Call outcome type | Normal completion vs. `Completed (Partial)` | Partial calls are scored on available signal only — see §5 |
| Sentiment | Derived from transcript tone across turns | Secondary modifier, not a primary driver |

---

## 3. Scoring model

**Approach:** a weighted rubric, not an opaque black-box score — so that a Lead Score is always explainable back to the signals in §2. Weights are configurable per campaign, since "qualified" means different things across campaigns.

**Default weighting (out of 100 points):**

| Component | Default weight |
|---|---|
| Interest level | 40 |
| Decision-maker status | 20 |
| Objection resolution | 15 |
| Script/discovery completion | 10 |
| Engagement quality | 10 |
| Explicit next step confirmed | 5 |

**Computation:** each component contributes a value between 0 and its weight, based on the captured signal (e.g. `interest_level = interested` contributes the full 40; `undecided` contributes ~20; `not_interested` contributes 0). Components sum to the Lead Score.

**Worked example:**

| Component | Signal | Points |
|---|---|---|
| Interest level | interested | 40 |
| Decision-maker status | sole | 20 |
| Objection resolution | one objection, addressed | 12 / 15 |
| Script completion | all required fields captured | 10 |
| Engagement quality | substantive, low interruption | 8 / 10 |
| Explicit next step | confirmed follow-up | 5 |
| **Total** | | **95** |

---

## 4. Bands and derived fields

### 4.1 Temperature tag
| Lead score range | Tag |
|---|---|
| 70–100 | Hot |
| 40–69 | Warm |
| 0–39 | Cold |

### 4.2 Qualification level
| Lead score range | Level |
|---|---|
| 85–100 | Highly Qualified |
| 60–84 | Qualified |
| 30–59 | Marginal |
| 0–29 | Unqualified |

### 4.3 Conversion probability
Expressed as a percentage, modeled from the same components as the Lead Score but calibrated against historical outcomes once available (i.e. what fraction of leads at a given score band actually converted downstream). Until sufficient historical data exists for a campaign, conversion probability defaults to a direct mapping from the Lead Score (`conversion_probability = lead_score%`) and should be recalibrated as outcome data accumulates.

Band thresholds (§4.1, §4.2) are configurable per campaign, since what counts as "hot" varies by product and sales cycle; the defaults above apply unless overridden.

---

## 5. Scoring partial (disconnected, non-recovered) calls

A call marked `Completed (Partial)` (Call State Machine, `CompletedPartial`) is still scored, per `FR-4.9`, but with two adjustments:

1. **Missing-field handling:** any required entity field still `not_captured` at the point of disconnect contributes zero to its component rather than being estimated — a partial call cannot be assumed to have gone as well as a completed one.
2. **Confidence flag:** the output includes a `score_confidence` indicator (`full` vs. `partial`) alongside the numeric score, so downstream consumers (Final Output, dashboard) can distinguish "a cold lead" from "we didn't get far enough to tell."

A call that disconnects before any interest signal is captured (e.g. during Opening) should score very low and carry `score_confidence = partial` — it must not default to a mid-range score simply because there's no negative signal either.

---

## 6. Edge cases

| Scenario | Handling |
|---|---|
| Contact explicitly asks for suppression (`requires_suppression = true`) | Score still computed for record-keeping, but qualification level is forced to `Unqualified` and excluded from active follow-up lists regardless of numeric score |
| Contact was clearly hostile / call ended in `end_call` from Opening | Lead score floors near 0; `score_confidence = partial` |
| Multiple attempts before terminal state | Scoring uses the terminal attempt's working memory, informed by `cumulative_entities` from contact history (Memory Specification §7) so earlier-attempt information isn't lost |
| Conflicting signals (e.g. `interest_level = interested` but multiple unaddressed objections) | Objection-resolution component still applies its own penalty independently — the model does not let one strong positive signal mask unresolved concerns |

---

## 7. Configurability

Exposed via the admin dashboard, per campaign:

- Component weights (§3)
- Temperature and qualification band thresholds (§4.1, §4.2)
- Which entity fields are treated as "required" for the script-completion component (ties to AI Agent Specification §2's `required_entity_fields`)

---

## 8. Traceability

| Element | Reference |
|---|---|
| Output fields | FRS FR-5.3 (Lead score field) |
| Input signals | Memory Specification §2 (`captured_entities`, `objections_raised`, `script_progress`, `turns`) |
| Partial-call handling | FRS FR-4.9, Call State Machine `CompletedPartial` |
| Suppression override | AI Agent Specification §8, Memory Specification §7 |

---

*No lost conversations. More opportunities. Higher conversion.*
