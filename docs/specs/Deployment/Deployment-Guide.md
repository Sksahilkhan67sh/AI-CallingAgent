# AI Calling Agent — Deployment Guide

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Infrastructure Architecture v1.0 · System Architecture v1.0 · Database Design v1.0 · Security Architecture v1.0 · Webhook Specification v1.0 · Test Plan v1.0

**Purpose:** Infrastructure Architecture §9 states the environment model and the rolling-deployment principle at a high level. This guide is the operational runbook — concrete steps, ordering, verification, and rollback — for standing up and updating the system in each environment.

---

## 1. Environments

| Environment | Purpose | Notes |
|---|---|---|
| Development | Feature work | External services mocked/sandboxed |
| Staging | Pre-production validation, load testing | Sandboxed telephony/STT/LLM/TTS providers; see Load Test Plan §3 |
| Production | Live campaigns | Full provider credentials, full monitoring |

Deploy in this order for every release: Development → Staging → Production. A release does not skip Staging, since Staging is where the Test Plan's exit criteria (§13) and the Load Test Plan's scenarios are actually executed.

---

## 2. Prerequisites before any deployment

- [ ] Test Plan §13 exit criteria met for this release scope (all Acceptance Criteria pass, State Machine suite passes, Release Gate regression cases pass, Threat Model risk-register items closed or explicitly accepted).
- [ ] Secrets for this environment present in the secrets manager / KMS (Security Architecture §5): telephony provider credentials, STT/LLM/TTS credentials, database credentials, webhook signing secret.
- [ ] Database migrations for this release reviewed and confirmed backward-compatible with the currently-running version (§5).
- [ ] Retry policy and agent configuration defaults confirmed for any new campaigns this release introduces (System Architecture, AI Agent Specification §10).

---

## 3. Deployment order

Deploy components in this order, since later components depend on earlier ones being available and schema-compatible:

1. **Database migrations** (§5) — schema changes land before any service that reads/writes the new shape.
2. **Data layer / Object Storage configuration changes**, if any.
3. **Stateless backend services**: Post-Call Analysis Workers, Queue Workers, Recovery Layer.
4. **Conversation Orchestrator** — deployed last among backend services, and via the rolling/draining procedure in §4, since it holds live call sessions.
5. **API Gateway / Admin Dashboard**.
6. **Webhook ingestion endpoints** — deploy only after the Recovery Layer and Queue Workers they trigger are already running the new version, so an inbound event during the deployment window is never handled by a version mismatch between the receiving endpoint and its downstream consumer.

---

## 4. Conversation Orchestrator rolling deployment

This is the most sensitive step, since a naive restart would forcibly disconnect every live call — which the system would then have to treat as a mid-call disconnect (Call State Machine, `DroppedMidCall`) at deployment-caused scale, unnecessarily exercising the recovery flow for every active call at once.

**Procedure:**
1. Mark new pods as available but not yet receiving new call assignments (drain-in / cordon equivalent for new traffic).
2. Stop routing **new** dial connections to old-version pods; old pods continue serving their existing in-progress calls only.
3. Wait for each old-version pod's in-progress calls to reach a terminal state naturally (call ends, or genuinely disconnects on its own) — do not force-terminate them for the sake of the deployment.
4. Once an old-version pod has zero active calls, terminate it.
5. Repeat until all pods are on the new version.

**Timeout guard:** set a maximum drain time (e.g. the campaign's longest expected call duration plus margin). If an old pod still has active calls after the timeout, escalate to a decision point (extend the drain, or accept forced termination as a last resort) rather than silently force-killing — a forced termination here is a deployment-caused disconnect and should be logged distinctly from a genuine network/provider disconnect for later analysis, even though it is handled by the same Stage 4 recovery flow (Infrastructure Architecture §9).

**Verification:** confirm zero calls were lost (i.e., every call active at deployment start either completed normally or entered the standard disconnect/recovery flow and reached a terminal state) — not just that the deployment itself succeeded.

---

## 5. Database migrations

- Migrations must be backward-compatible with the currently-running application version at the moment they run, since deployment order (§3) applies the migration before the new application code is live.
- A schema change to any table in Database Design (e.g. adding a column to `call_attempt`) should ship as an additive, nullable change first; removing or repurposing a column happens in a later release once no running version depends on the old shape.
- The `working_memory_snapshot` schema is versioned independently (Memory Specification §8) specifically so a schema evolution here does not require every in-flight snapshot to be migrated synchronously — old-version snapshots remain readable by the migration/compatibility logic described there.
- Run migrations against a Staging copy of production-shaped (but synthetic) data before Production, to catch migration failures before they affect live campaigns.

---

## 6. Secrets and configuration rollout

- New or rotated secrets (telephony credentials, webhook signing secret, provider API keys) are loaded into the secrets manager before the services that need them are deployed — never bundled into the deployment artifact itself (Security Architecture §5).
- A webhook signing secret rotation follows Security Architecture §5's dual-secret support: both old and new secrets are valid for a transition window so in-flight webhook deliveries signed with the old secret aren't rejected mid-rotation.
- Retry policy and agent configuration changes (Admin Dashboard writes) are **not** part of a code deployment — they take effect immediately per System Architecture's "configuration without deployment" principle and are out of scope for this guide's deployment steps.

---

## 7. Post-deployment verification

Run in every environment after deployment completes, before considering the release final:

- [ ] Health checks green for all services in §3's deployment order.
- [ ] A synthetic end-to-end test call completes successfully (normal path).
- [ ] A synthetic disconnect-and-reconnect test call completes successfully (recovery path) — confirms the deployment didn't regress the "no lost conversations" guarantee.
- [ ] Webhook endpoints accept a validly-signed test event and reject an invalidly-signed one.
- [ ] Dashboard reflects live campaign status correctly.
- [ ] No unexpected spike in error rate, disconnect rate, or reconnect-failure rate on the metrics defined in Infrastructure Architecture §8, compared to pre-deployment baseline.

---

## 8. Rollback

- **Stateless services** (Queue Workers, Recovery Layer, Post-Call Analysis Workers, API Gateway): roll back by redeploying the previous version; no special sequencing beyond reversing §3's order for the affected services.
- **Conversation Orchestrator**: rollback follows the same drain procedure as §4, in reverse — new (bad) version pods stop receiving new calls, old (known-good) version pods take over new dial connections, bad-version pods drain and terminate.
- **Database migrations**: because migrations are required to be backward-compatible (§5) with the previous application version, a rollback of application code does not require reversing the migration in the common case. A migration that must itself be rolled back (rare, and ideally avoided by the additive-first practice in §5) requires its own reviewed down-migration, tested in Staging before being run in Production.
- **Secrets/config rollback**: revert to the previous configuration value; for a secret rotation rollback, keep both old and new secrets valid during the reversal window, same as forward rotation (§6).

---

## 9. Monitoring during and after deployment

Watch the metrics defined in Infrastructure Architecture §8 with heightened attention during the deployment window and for a defined post-deployment observation period:

- Disconnect rate and reconnect-success rate (Infrastructure Architecture §8) — a deployment-caused spike here should match the expected pattern from §4's drain procedure, not exceed it.
- Per-turn conversation latency — a regression here during/after an Orchestrator deployment indicates a problem with the new version, not just deployment mechanics.
- Webhook accept/error rate — a spike in `401`/`422` responses after deployment may indicate a signature or schema mismatch introduced by the release.

---

## 10. Traceability

| Deployment element | Reference |
|---|---|
| Environment model | Infrastructure Architecture §9 |
| Rolling deployment principle (origin) | Infrastructure Architecture §9 |
| Drain-before-terminate detail | This document §4, informed by Call State Machine (`DroppedMidCall` handling) |
| Migration compatibility | Database Design, Memory Specification §8 |
| Secrets rotation | Security Architecture §5 |
| Release exit criteria | Test Plan §13 |
| Metrics watched | Infrastructure Architecture §8 |

---

*No lost conversations. More opportunities. Higher conversion.*
