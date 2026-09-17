# AI Calling Agent — Rollback Runbook

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Deployment Guide v1.0 (§8) · Infrastructure Architecture v1.0 · Database Design v1.0 · Memory Specification v1.0 · Monitoring v1.0 · Incident Runbook v1.0

**Purpose:** Deployment Guide §8 states the rollback approach per component type at a summary level. This document is the executable runbook — the decision to roll back, the exact steps per component, and how to verify the rollback actually fixed things rather than just reverting a version number.

**How to use this document:** like the Incident Runbook, this is written to be followed in real time during a rollback, not read afterward for background.

---

## 1. Rollback vs. fix-forward — the decision

Not every post-deployment problem should trigger a rollback. Decide using this order of checks:

1. **Is a Critical alert firing** (Monitoring §4) that traces to this deployment? → Roll back. Don't attempt a fix-forward under active Critical incident pressure (Incident Runbook §1's suppression scenario is the clearest example: contain first, root-cause later).
2. **Did Deployment Guide §7's post-deployment verification fail** on any checklist item? → Roll back, unless the failure is trivially and immediately fixable (e.g. a dashboard display bug with no functional impact) — in which case fix-forward is acceptable with explicit sign-off from the engineering lead.
3. **Is the problem isolated to a non-critical, easily-flagged-off feature** (e.g. a feature flag from Environment Config §2.7)? → Prefer disabling the flag over a full rollback, since it's faster and lower-risk than reversing a whole deployment.
4. **Is the problem in the Conversation Orchestrator specifically?** → Rollback here is the most expensive and highest-risk option (§4 below); confirm the problem can't be mitigated by scaling or config change first, per Incident Runbook §5's guidance.

**Default when in doubt:** roll back. A reverted deployment is cheaper to recover from than an extended incident on a system whose core promise is "no lost conversations."

---

## 2. Rollback authority

- Any on-call engineer can **initiate** a rollback for a Critical alert (§1.1) without prior approval — waiting for sign-off during an active Critical incident is itself a risk.
- A fix-forward decision under §1.2's exception requires engineering lead sign-off, logged in the incident channel.
- A rollback for a non-Critical, non-verification-failure reason (e.g. a business decision to delay a feature) should go through normal change-management, not this runbook.

---

## 3. Pre-rollback checklist

Before executing any rollback step:

- [ ] Identify the last known-good version/tag for every component being rolled back.
- [ ] Confirm whether a database migration shipped with the deployment being rolled back (§5) — this determines whether data-layer rollback steps are needed at all.
- [ ] Note the current state of any in-flight campaigns (active call count, current concurrency) before touching the Conversation Orchestrator (§4).
- [ ] Announce the rollback is starting in the incident channel, referencing the triggering alert or verification failure.

---

## 4. Rollback: stateless services

**Applies to:** Queue Workers, Recovery Layer, Post-Call Analysis Workers, API Gateway, Admin Dashboard.

**Steps:**
1. Redeploy the previous known-good version for the affected service(s).
2. Reverse Deployment Guide §3's ordering for just the affected services — if only the API Gateway is being rolled back, backend services don't need to move.
3. Confirm health checks green on the rolled-back version.

**Verification:** re-run the relevant subset of Deployment Guide §7's post-deployment checklist for the affected service (e.g. if API Gateway was rolled back, re-verify auth and dashboard checks, not the full end-to-end call tests).

---

## 5. Rollback: Conversation Orchestrator

This mirrors the forward deployment's drain procedure (Deployment Guide §4) in reverse, and carries the same risk profile — it must not forcibly disconnect live calls if avoidable.

**Steps:**
1. Stop routing **new** dial connections to the current (bad) version's pods.
2. Begin routing new dial connections to the previous known-good version's pods instead — these should still be available if the forward deployment's old pods haven't been fully terminated yet; if they have been, redeploy the previous version fresh.
3. Allow the bad version's in-progress calls to reach a terminal state naturally, exactly as in the forward drain procedure — do not force-terminate for rollback speed alone.
4. Apply the same maximum drain timeout guard as Deployment Guide §4; if exceeded, escalate to a forced-termination decision rather than silently doing it.
5. Once the bad version has zero active calls, terminate its pods entirely.

**Special case — rollback triggered by a live Critical incident (e.g. Monitoring §2.1's disconnect rate spike traced to this deployment):** if waiting for a natural drain would prolong an active Critical incident unacceptably, forced termination of the bad version's remaining calls is justified — but this must be explicitly decided and logged as such (not defaulted into), since it converts an unknown number of calls into `DroppedMidCall` events all at once. Confirm the standard Stage 4 recovery flow is functioning normally *before* doing this, so those forced disconnects are actually recoverable rather than being made worse.

**Verification:**
- Confirm zero calls were lost per Deployment Guide §7's disconnect-and-reconnect verification logic — every call active during the rollback either completed normally or entered recovery and reached a terminal state.
- Confirm `per_turn_latency_p95` (Monitoring §2.1) returns to the previous baseline associated with the known-good version.

---

## 6. Rollback: database migrations

**Default assumption:** because migrations are required to be additive/backward-compatible (Deployment Guide §5), rolling back the application code does **not** require reversing the migration in the common case — the previous application version should still function correctly against the new (additive) schema.

**Only reverse a migration if:**
- The migration itself is the confirmed root cause (not just the application code that used it), and
- A reviewed down-migration exists and has been tested in Staging.

**Steps (only when reversal is actually required):**
1. Confirm no component still depends on the migrated shape (check that all application rollbacks in §4–§5 have completed first).
2. Run the down-migration against Staging first if there's any doubt, even mid-incident, since a failed down-migration against Production is worse than the original problem.
3. Run the down-migration against Production.
4. Verify data integrity — particularly for any table holding `suppression`, `working_memory_snapshot`, or `final_output` data, given their compliance/correctness sensitivity (Database Design §2.9–2.13).

**If a down-migration doesn't exist and can't be safely improvised:** do not attempt one live during an incident. Roll back application code only, and accept the additive schema change remains in place until a proper down-migration can be authored and tested.

---

## 7. Rollback: secrets and configuration

**Steps:**
1. Revert the environment variable/secret value to its previous state in the secrets manager (Environment Config §2).
2. For a webhook signing secret rotation specifically: keep **both** the old and the rolled-back-to secret valid during the reversal window, mirroring the forward-rotation dual-secret support (Security Architecture §5, Environment Config §2.2) — an abrupt single-secret cutover during a rollback recreates the exact failure mode described in Incident Runbook §3.
3. Confirm no service is still caching the old (bad) configuration value past its expected refresh interval.

**Verification:** re-run the webhook signature test (Deployment Guide §7) against both old and new secret to confirm the transition window is genuinely accepting both.

---

## 8. Post-rollback verification (full)

Run the complete Deployment Guide §7 checklist after any rollback affecting the Conversation Orchestrator, database, or webhook ingestion — not just the reduced subset in §4 above, since a rollback that touches the recovery-critical path deserves the same scrutiny as a forward deployment:

- [ ] Health checks green across all services.
- [ ] Synthetic normal-path test call succeeds.
- [ ] Synthetic disconnect-and-reconnect test call succeeds.
- [ ] Webhook endpoints accept valid signatures and reject invalid ones.
- [ ] Dashboard reflects live status correctly.
- [ ] Monitoring §2's metrics return to pre-incident baseline.

---

## 9. Communication

- Announce rollback start and completion in the incident channel, referencing the triggering alert (Monitoring §4) or verification failure (Deployment Guide §7).
- If the rollback was triggered by a Critical alert with compliance implications (e.g. Incident Runbook §1's suppression scenario), the same compliance/legal stakeholder notified during the incident should also be told once the rollback completes and verification (§8) passes.
- If campaign managers were told about degraded service during the incident (Incident Runbook §9's outage-communication gap notwithstanding), confirm they're also told once resolved.

---

## 10. Postmortem requirement

- Any rollback triggered by a Critical alert follows that alert's postmortem requirement as defined in Incident Runbook (e.g. mandatory for suppression, webhook-signature, and confirmed ordering-invariant incidents).
- Any rollback at all — even a smooth, low-drama one — gets a brief retrospective note: what triggered it, what was rolled back, and whether the pre-rollback checklist (§3) and verification (§8) were sufficient or need revision. This is lighter than a full postmortem but should not be skipped, since rollback-procedure gaps are exactly the kind of thing that's invisible until the one time they matter.

---

## 11. Traceability

| Rollback element | Reference |
|---|---|
| Origin / summary version | Deployment Guide §8 |
| Orchestrator drain procedure (forward direction) | Deployment Guide §4 |
| Migration compatibility requirement | Deployment Guide §5, Memory Specification §8 |
| Secret dual-validity requirement | Security Architecture §5 |
| Alert-triggered rollback criteria | Monitoring §4, Incident Runbook (all sections) |
| Post-rollback verification checklist | Deployment Guide §7 |

---

*No lost conversations. More opportunities. Higher conversion.*
