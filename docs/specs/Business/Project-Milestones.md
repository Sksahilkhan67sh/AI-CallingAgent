# AI Calling Agent — Project Milestones

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready (documentation)

**Related documents:** all documents listed below.

> **Note on dates:** this plan sequences work by dependency, not by calendar date — actual scheduling depends on team size and capacity, which this document doesn't assume. Relative effort is shown as a size (S/M/L) rather than a fabricated deadline. Fill in real dates once the team and start date are known.

---

## 1. Phase overview

| Phase | Goal | Exit criteria |
|---|---|---|
| 0 — Discovery & Definition | Agree what's being built and why | PRD, FRS, User Stories, Acceptance Criteria approved by stakeholders |
| 1 — Design & Architecture | Agree how it's built | System, data, API, and AI-behavior designs reviewed and signed off |
| 2 — Security & Compliance Review | Confirm the design is safe to build | Threat Model risks identified with mitigation plans; privacy/consent behavior agreed |
| 3 — Build | Implement against the design | Feature-complete build matching FRS scope |
| 4 — Testing & Hardening | Prove it works and survives failure | Test Plan exit criteria met (Test Plan §13) |
| 5 — Launch Readiness | Prove it can be run in production | Deployment, monitoring, and incident procedures rehearsed |
| 6 — Post-Launch | Keep it working and improving | Monitoring live; first real QA/incident cycle completed |

---

## 2. Phase 0 — Discovery & Definition

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M0.1 | Product intent agreed | PRD | — | M |
| M0.2 | Testable requirements defined | FRS | M0.1 | L |
| M0.3 | User-facing behavior defined | User Stories | M0.2 | M |
| M0.4 | Test-level acceptance criteria defined | Acceptance Criteria | M0.3 | M |

**Phase 0 exit gate:** stakeholders (product, engineering, QA leads) sign off on PRD + FRS together — FRS should not be considered final until PRD is, since FRS derives from it.

---

## 3. Phase 1 — Design & Architecture

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M1.1 | Workflow and state model defined | Detailed Workflow, Call State Machine | M0.2 | M |
| M1.2 | System components defined | System Architecture | M1.1 | M |
| M1.3 | Data model defined | Database Design, ER Diagram | M1.2 | L |
| M1.4 | External interfaces defined | API Specification, Webhook Specification | M1.3 | L |
| M1.5 | Infrastructure/deployment topology defined | Infrastructure Architecture | M1.2 | M |
| M1.6 | AI agent behavior defined | AI Agent Specification, Conversation Flow, Prompt Specification | M1.1 | L |
| M1.7 | Conversation memory model defined | Memory Specification | M1.6, M1.3 | M |
| M1.8 | Lead scoring model defined | Lead Scoring Specification | M1.7 | M |

**Phase 1 exit gate:** engineering can point to a single design for every FRS requirement — no requirement should be traceable to zero design documents (spot-check against the traceability tables already embedded in each doc).

---

## 4. Phase 2 — Security & Compliance Review

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M2.1 | Technical security controls defined | Security Architecture | M1.5 | M |
| M2.2 | Privacy posture defined | Data Privacy | M2.1 | M |
| M2.3 | Recording consent behavior defined | Recording Consent | M2.2, M1.6 | M |
| M2.4 | Threats formally analyzed | Threat Model | M2.1 | L |

**Phase 2 exit gate:** every item in the Threat Model's risk register has an assigned owner and a planned mitigation (even if the mitigation itself lands in Phase 4) — an unowned Critical/High risk blocks moving to Phase 3.

**Note:** Phase 2 can run partially in parallel with the later half of Phase 1 (e.g. M2.1 can start once M1.5 is stable, without waiting for M1.8) — security review of the infrastructure design doesn't need the lead-scoring model finished.

---

## 5. Phase 3 — Build

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M3.1 | Calling system implemented (Stage 1) | Working code against FRS Module 1 | M1.3, M1.4 | L |
| M3.2 | Conversation engine implemented (Stage 2) | Working code against FRS Module 2, AI Agent Spec, Prompt Spec | M1.6, M1.7 | L |
| M3.3 | Data & admin implemented (Stage 3) | Working code against FRS Module 3 | M3.1 | M |
| M3.4 | Disconnect & recovery implemented (Stage 4) | Working code against FRS Module 4, Memory Spec | M3.2 | L |
| M3.5 | Final output & lead scoring implemented (Stage 5) | Working code against FRS Module 5, Lead Scoring Spec | M3.3, M3.4 | M |
| M3.6 | Retry policy implemented (Module 6) | Working code against FRS Module 6 | M3.1, M3.4 | S |
| M3.7 | Security controls implemented | Working code against Security Architecture | M2.1, M3.1–M3.6 | M |
| M3.8 | Recording consent flow implemented | Working code against Recording Consent | M2.3, M3.2 | M |

**Phase 3 exit gate:** feature-complete against FRS; unit and integration tests (Test Plan §1) passing continuously, not deferred to Phase 4.

---

## 6. Phase 4 — Testing & Hardening

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M4.1 | Functional test suite executed | Test-Cases.xlsx (Epics 1–6) results | M3.1–M3.6 | M |
| M4.2 | State machine & webhook suites executed | Test-Cases.xlsx (State Machine, Webhooks) results | M3.4, M3.1 | M |
| M4.3 | Security & consent suites executed | Test-Cases.xlsx (Security, Recording Consent) results | M3.7, M3.8 | M |
| M4.4 | Release-gate regression suite executed | Test-Cases.xlsx (Regression) results — must pass | M4.1–M4.3 | S |
| M4.5 | Load and chaos scenarios executed | Load Test Plan results | M3.1–M3.6 stable in Staging | L |
| M4.6 | First QA report generated | QA-Report.md (Cycle 1, real results) | M4.1–M4.5 | S |

**Phase 4 exit gate:** Test Plan §13's exit criteria fully met, including the Release-Gate suite (M4.4) passing and every Threat Model risk-register item closed or explicitly accepted (cross-check against QA-Report.md §4).

---

## 7. Phase 5 — Launch Readiness

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M5.1 | Environments and secrets provisioned | Environment Config applied | M1.5 | M |
| M5.2 | Deployment procedure rehearsed in Staging | Deployment Guide followed end-to-end at least once | M4.6, M5.1 | M |
| M5.3 | Rollback procedure rehearsed | Rollback Runbook followed at least once (a drill, not a real incident) | M5.2 | S |
| M5.4 | Monitoring and alerting live | Monitoring dashboards/alerts configured per catalog | M5.1 | M |
| M5.5 | Incident runbook walked through | Incident Runbook reviewed with on-call team, at least a tabletop exercise for the Critical scenarios | M5.4 | S |
| M5.6 | Capacity sized for launch volume | Capacity-Planning.xlsx current-state sizing applied | M4.5 | S |
| M5.7 | Cost baseline established | Cost-Model.xlsx baseline run against actual launch assumptions | M5.6 | S |

**Phase 5 exit gate:** a production deployment has been rehearsed (not just documented) at least once, including a rollback drill — the first real production deployment should not be the first time the procedure is executed.

---

## 8. Phase 6 — Post-Launch

| ID | Milestone | Deliverable(s) | Depends on | Size |
|---|---|---|---|---|
| M6.1 | First live campaign monitored end-to-end | Monitoring data reviewed against SLOs | M5.4, launch | M |
| M6.2 | First real incident (if any) run through the runbook | Incident Runbook applied for real, postmortem written if required | M6.1 | Varies |
| M6.3 | Threat Model reassessed against real traffic patterns | Updated Threat Model, especially risk #2 (outage-shaped flood) validated against real provider behavior | M6.1 | M |
| M6.4 | Capacity/cost model recalibrated with real usage data | Capacity-Planning.xlsx and Cost-Model.xlsx re-run with actual (not assumed) volume/rates | M6.1 | S |
| M6.5 | Growth-projection checkpoint | Compare Capacity-Planning.xlsx's Growth Projection sheet against actual month-over-month growth; adjust pod/worker provisioning ahead of the next projected threshold-crossing month | M6.4 | S |

---

## 9. Cross-phase dependency notes

- Phase 2 (Security review) does not need to fully complete before Phase 3 (Build) starts, but **must** complete before Phase 4's security test suite (M4.3) can be meaningfully run — building without a design-level threat model is riskier than deferring build, but testing security behavior that was never threat-modeled is not meaningful at all.
- The Release-Gate regression suite (M4.4) is the single hardest dependency in this plan: it cannot be marked done by any means other than actually passing (Test Plan §13) — no milestone downstream of it should be started assuming it will pass.
- Phase 5's rehearsal milestones (M5.2, M5.3, M5.5) are frequently the ones skipped under schedule pressure; this plan calls them out explicitly as exit-gate items specifically because "we documented the procedure" is not equivalent to "we've run the procedure."

---

## 10. Traceability

Every deliverable named above is itself the traceability link — this document doesn't introduce new specification content, only sequencing. If a milestone's deliverable is unclear, refer to that document's own Purpose section.

---

*No lost conversations. More opportunities. Higher conversion.*
