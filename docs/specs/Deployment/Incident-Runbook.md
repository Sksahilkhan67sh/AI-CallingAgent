# AI Calling Agent — Incident Runbook

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Monitoring v1.0 · Security Architecture v1.0 (§11) · Threat Model v1.0 (§11) · Webhook Specification v1.0 · Memory Specification v1.0 · Infrastructure Architecture v1.0

**Purpose:** Monitoring's alert catalog says *what* fires and at what severity. Security Architecture §11 and Threat Model §11 sketch *that* an incident response exists. This document is the actual runbook — concrete steps for whoever is paged, for each Critical/High alert.

**How to use this document:** each section is written to be followed in real time during an incident, not read for background afterward. Sections are ordered: Trigger → Immediate Actions (first 5 minutes) → Diagnosis → Mitigation → Escalation → Resolution criteria → Postmortem requirement.

---

## 1. Suppression check failure

**Trigger:** `suppression_check_failures` > 0 (Monitoring §2.3). **Severity: Critical.**

**Immediate actions (first 5 min):**
1. Confirm the alert is real, not a metric/monitoring bug: pull the specific contact ID(s) flagged and check their `suppression` table row and dial log directly.
2. If confirmed, **pause the affected campaign(s)** immediately via the Admin Dashboard (Detailed Workflow does not define a "pause all" action at the contact level — pausing at the campaign level is the fastest available containment).
3. Notify the compliance/legal stakeholder for this system, not just engineering — this is a compliance incident (Security Architecture §11), separate from an ordinary bug.

**Diagnosis:**
- Determine which enforcement path failed: initial queue entry, a retry re-entry (Security Architecture §8), or the phone-number-vs-contact-ID matching (Database Design §2.12).
- Check whether the failure is isolated (one contact, likely a data bug) or systemic (a code path bypassing the check entirely, likely a regression from a recent deployment — cross-reference Deployment Guide §7's release history).

**Mitigation:**
- If systemic and deployment-related: roll back per Deployment Guide §8.
- If isolated: manually confirm no further contact occurs for the affected number(s), and add a corrective suppression entry if the original write failed to persist.

**Escalation:** page engineering lead and compliance stakeholder immediately; do not wait for diagnosis to complete before notifying compliance.

**Resolution criteria:** root cause identified, fix deployed and verified via TC-REG.01–TC-REG.03 (Test-Cases.xlsx) passing against the fixed code, and confirmation that no additional contacts were affected beyond those already identified.

**Postmortem:** mandatory, regardless of how quickly resolved — this incident class always gets a full writeup given its compliance weight (Threat Model risk #1).

---

## 2. Disconnect rate spike

**Trigger:** `disconnect_rate` > 30%, sustained 5+ min (Monitoring §2.1). **Severity: Critical.**

**Immediate actions:**
1. Check whether the spike correlates with a specific telephony provider, region, or campaign — a spike isolated to one provider suggests a provider-side outage rather than an internal regression.
2. Check `webhook_accept_rate` and `webhook_signature_failure_rate` simultaneously — a real provider outage often produces both a disconnect spike and a webhook volume spike together (Load Test Plan §4.3's exact scenario).

**Diagnosis:**
- Provider-side outage: confirm via the provider's own status page/API if available.
- Internal regression: check recent deployments (Deployment Guide §3) to the Conversation Orchestrator or telephony integration layer.
- Network/infrastructure issue: check Infrastructure Architecture's regional placement (§5) for a regional outage affecting the Orchestrator-to-provider path.

**Mitigation:**
- Provider outage: this is expected to be absorbed by the existing recovery flow (Stage 4) and retry policy — confirm `reconnect_success_rate` is still functioning (it should be, since reconnect logic doesn't depend on the specific disconnect volume). Do not disable retry policy or recovery logic as a "fix" — that would defeat the system's core guarantee at exactly the moment it's needed most.
- Internal regression: roll back per Deployment Guide §8.
- If webhook ingestion itself is dropping events under the volume (Threat Model risk #2): this is the failure mode Load Test Plan §4.3 exists to have already caught; if it's happening in production, treat as a P0 capacity/rate-limit misconfiguration and adjust `WEBHOOK_RATE_LIMIT_PER_MINUTE` (Environment Config §4) as an emergency mitigation, then schedule a proper re-run of Load Test Plan §4.3 post-incident.

**Escalation:** page on-call engineering; page infrastructure/platform lead if root cause is capacity-related.

**Resolution criteria:** `disconnect_rate` returns to within 10% of 7-day baseline, sustained for 30+ min.

**Postmortem:** required if root cause was internal (regression, misconfiguration); optional-but-recommended if purely external provider outage with no internal contributing factor.

---

## 3. Webhook signature failure spike

**Trigger:** sustained spike in `webhook_signature_failure_rate` (Monitoring §2.2). **Severity: Critical.**

**Immediate actions:**
1. Check whether a secret rotation (Security Architecture §5, Deployment Guide §6) occurred recently — this is the most common legitimate cause.
2. If a rotation is in progress or recently completed, verify the dual-secret transition window (§6 of both documents) is actually configured correctly — this is very likely an operational misconfiguration, not an attack.
3. If no rotation occurred, treat as a potential attack or provider-side signing change and do not assume benign cause.

**Diagnosis:**
- Pull a sample of failing requests and confirm which secret they were signed with (old vs. new vs. neither).
- Check provider-side documentation/status for any signing-method change on their end.

**Mitigation:**
- Misconfigured rotation: restore the old secret as valid immediately (should already be valid if the dual-secret window was implemented correctly — this confirms whether it was).
- Genuine attack: block the source at the network layer if identifiable; this does not stop legitimate provider traffic since that traffic is signed correctly.

**Escalation:** page on-call immediately; page security lead if attack is suspected rather than misconfiguration.

**Resolution criteria:** signature failure rate returns to near-zero baseline; legitimate `call.disconnected` events during the incident window are confirmed not to have been lost (cross-check against Deployment Guide §7-style verification: were any calls active during the incident left without a resolved terminal state?).

**Postmortem:** mandatory — per Webhook Specification §9, this event type is explicitly called out as one where failing quietly would be an operational emergency.

---

## 4. Disconnect-to-snapshot latency breach

**Trigger:** p95 of `disconnect_to_snapshot_latency` exceeds target (Monitoring §2.2). **Severity: High.**

**Immediate actions:**
1. Check Memory Store health/latency directly (Infrastructure Architecture §4) — this is the most likely bottleneck.
2. Check current disconnect volume — is this correlated with an ongoing disconnect-rate spike (§2 of this runbook)?

**Diagnosis:**
- Memory Store under load or degraded: check its own metrics/capacity.
- State Saver service under-provisioned: check autoscaling behavior (Infrastructure Architecture §2).

**Mitigation:**
- Scale the Memory Store or State Saver capacity per Infrastructure Architecture §6's scaling guidance.
- If this is happening under a simultaneous disconnect-rate spike, this may be the same root incident as §2 above — don't treat as fully independent without checking correlation first.

**Escalation:** page on-call; this does not require immediate compliance/legal notification unless it's confirmed to have caused an actual ordering-invariant violation (i.e., a retry decision made before a snapshot committed) rather than just elevated latency within still-safe bounds.

**Resolution criteria:** p95 returns within target; confirm via logs/traces (Monitoring §7) that no retry decision was made against a not-yet-durable snapshot during the incident window.

**Postmortem:** required if any ordering-invariant violation is found; otherwise a lighter capacity-review note suffices.

---

## 5. Per-turn conversation latency breach

**Trigger:** p95 of `per_turn_latency` exceeds budget, 5+ min (Monitoring §2.1). **Severity: High.**

**Immediate actions:**
1. Break down latency by stage (STT / LLM / TTS) using the per-turn trace (Monitoring §7) to isolate which external service or internal step is responsible.
2. Check whether the affected stage's provider has a known status/incident.

**Diagnosis:**
- External provider (STT/LLM/TTS) degradation: confirmed via the trace breakdown showing one stage dominating the latency.
- Internal Orchestrator scaling lag: check pod count vs. concurrent-call count (Infrastructure Architecture §2).
- Regional placement issue: check whether traffic is unexpectedly crossing regions (Infrastructure Architecture §5).

**Mitigation:**
- Provider degradation: no direct fix available internally; monitor and communicate expected impact; consider provider failover if a secondary is configured (not assumed present in this documentation set — flag as a gap if not).
- Orchestrator scaling lag: manually scale ahead of autoscaler reaction time if the situation is time-critical.

**Escalation:** page on-call; escalate to provider support channel if external.

**Resolution criteria:** p95 back within budget for 30+ min.

**Postmortem:** recommended, especially if it revealed a scaling-configuration gap.

---

## 6. Reconnect success rate drop

**Trigger:** `reconnect_success_rate` below 80% (Monitoring §2.1). **Severity: Medium** (pages on-call, not necessarily after-hours unless during peak calling window).

**Immediate actions:**
1. Check whether this correlates with the disconnect-rate spike runbook (§2) — a genuine provider outage naturally depresses reconnect success too, since the same conditions causing disconnects also hinder redialing.
2. Check Reconnect Manager logs for a specific failure pattern (e.g. memory reload failures vs. dial failures).

**Diagnosis:**
- If dial failures dominate: likely the same provider issue as §2.
- If memory reload failures dominate: check Memory Store health and the working-memory snapshot read path (Memory Specification §3).

**Mitigation:** same provider-outage guidance as §2 if applicable; otherwise, investigate and fix the memory-reload path specifically.

**Escalation:** standard on-call; escalate to engineering lead if isolated to memory-reload (a correctness bug, not just an availability blip).

**Resolution criteria:** rate returns above 80% sustained.

**Postmortem:** optional unless a memory-reload correctness bug is found, in which case mandatory (touches the "no lost conversations" guarantee directly).

---

## 7. Queue backlog growth

**Trigger:** `queue_backlog_size` trending up 30+ min without plateau (Monitoring §2.1). **Severity: Medium.**

**Immediate actions:**
1. Check Queue Worker pod count and autoscaling status.
2. Check whether this coincides with a retry burst (Load Test Plan §4.4's scenario occurring organically rather than as a test).

**Diagnosis:** under-provisioned Queue Workers vs. a genuine large simultaneous-eligibility retry wave vs. a downstream bottleneck (telephony provider throughput limit).

**Mitigation:** scale Queue Workers; if telephony provider throughput is the actual limit, this is a capacity/contract discussion, not an engineering fix.

**Escalation:** standard on-call.

**Resolution criteria:** backlog stabilizes or drains.

**Postmortem:** not required unless it caused missed calling-window commitments.

---

## 8. PII exposure incident

**Trigger:** manual discovery (e.g. misconfigured access control found exposing recordings/transcripts) — not currently a Monitoring-catalog alert; flagged here as a gap (see §10). **Severity: Critical.**

**Immediate actions:**
1. Contain: revoke/correct the exposing access control immediately.
2. Do not wait for full scope assessment before containment.

**Diagnosis:** determine scope — which records, how long exposed, who could have accessed them (Security Architecture §9's audit logs are the primary tool here).

**Mitigation:** per the organization's standard data-breach response process (Security Architecture §11 notes this is out of this documentation set's scope but must be linked once defined — **this remains an open item**, see §10).

**Escalation:** security lead, legal/compliance, and organizational incident command immediately.

**Resolution criteria:** defined by the linked organizational process (§10 gap).

**Postmortem:** mandatory, per standard data-breach practice.

---

## 9. Provider outage (telephony/STT/LLM/TTS)

**Trigger:** correlated signals across §2 (disconnect rate), §5 (per-turn latency), and/or direct provider status notification. **Severity: High-Critical depending on scope.**

**Immediate actions:**
1. Confirm via provider status channel if available.
2. Communicate expected impact to campaign managers via the Admin Dashboard status (if a system-wide banner/notice capability exists — flagged as a possible gap if not, see §10).

**Diagnosis:** scope (single provider, single region, all traffic) and expected duration if the provider has communicated one.

**Mitigation:** rely on existing retry policy and recovery flow (Stage 4) to absorb the outage's effect on individual calls, per §2's guidance not to disable core recovery logic. If a secondary/failover provider exists, engage it (not assumed present — flag as a gap if not, see §10).

**Escalation:** on-call, plus provider account/support escalation path.

**Resolution criteria:** provider confirms resolution; internal metrics (§2, §5) confirm return to baseline.

**Postmortem:** recommended, particularly to capture whether the retry policy and recovery flow performed as expected under real (not simulated) outage conditions — this is valuable input back into Load Test Plan §4.3's ongoing validity.

---

## 10. Known gaps in this runbook

Noted honestly rather than assumed away:

- No defined organizational data-breach response process is linked yet (§8) — this runbook's PII exposure section ends at containment and escalation, not full resolution.
- No secondary/failover provider strategy is documented in this specification set (§9) — if one exists operationally, it should be added here; if not, it's a business continuity gap worth raising separately.
- No system-wide status/notice capability for campaign managers during a provider outage is confirmed to exist (§9) — worth confirming with the Admin Dashboard's actual feature set.

---

## 11. Traceability

| Runbook section | Reference |
|---|---|
| Alert definitions | Monitoring v1.0 §4 |
| Suppression incident | Threat Model risk #1, Security Architecture §7–§8, §11 |
| Webhook signature incident | Webhook Specification §9 |
| Ordering-invariant check | Memory Specification §5 |
| Deployment rollback steps | Deployment Guide §8 |

---

*No lost conversations. More opportunities. Higher conversion.*
