# AI Calling Agent — Monitoring

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Infrastructure Architecture v1.0 (§8) · Deployment Guide v1.0 (§9) · Threat Model v1.0 · PRD v1.0 (§9 Success metrics) · Load Test Plan v1.0 · Environment Config v1.0

**Purpose:** Infrastructure Architecture §8 names the observability pillars; this document is the full specification — the metrics catalog with definitions and thresholds, the alert rules and routing, logging standards, and on-call procedures.

---

## 1. Observability pillars

| Pillar | Purpose |
|---|---|
| Metrics | Aggregate system health and business KPIs over time |
| Logs | Per-event detail for debugging and audit (Security Architecture §9) |
| Traces | Per-call, per-turn latency breakdown across STT/LLM/TTS |

---

## 2. Metrics catalog

### 2.1 Call pipeline health

| Metric | Definition | Source | Alert threshold |
|---|---|---|---|
| `connection_rate` | Connected calls ÷ total dial attempts | Calling Queue | Warn if drops >10% below 7-day baseline |
| `disconnect_rate` | Unexpected disconnects ÷ connected calls | Conversation Orchestrator | Warn at >15%, page at >30% (sustained 5+ min) |
| `reconnect_success_rate` | Successful reconnects ÷ retry-approved disconnects | Reconnect Manager | Warn if drops below 80% |
| `retry_exhaustion_rate` | Contacts closed via `Closed` or `CompletedPartial` with retries exhausted ÷ total contacts | Contact/Call Attempt tables | Informational — tracked per PRD §9, not typically paged |
| `per_turn_latency_p95` | 95th-percentile time from utterance-complete to response-spoken-start | Conversation Orchestrator | Page if p95 exceeds the budget in Infrastructure Architecture §5 for 5+ min |
| `queue_backlog_size` | Contacts in `Pending`/`RetryScheduled` awaiting dispatch | Calling Queue | Warn if trending upward for 30+ min without plateauing (Load Test Plan §4.1 pass criteria, applied continuously) |

### 2.2 Webhook & recovery pipeline

| Metric | Definition | Source | Alert threshold |
|---|---|---|---|
| `webhook_accept_rate` | Accepted (`200`) ÷ total webhook requests | Webhook ingestion | Warn if drops below 95% |
| `webhook_signature_failure_rate` | `401` responses ÷ total webhook requests | Webhook ingestion | Page on sustained spike (Webhook Specification §9 — could indicate secret rotation failure or attack) |
| `webhook_schema_failure_rate` | `422` responses ÷ total webhook requests | Webhook ingestion | Warn — may indicate a provider-side integration change |
| `disconnect_to_snapshot_latency` | Time from `call.disconnected` receipt to durable snapshot commit | State Saver | Page if p95 exceeds target (this is the ordering-invariant-adjacent metric from Memory Specification §5) |

### 2.3 Data & security

| Metric | Definition | Source | Alert threshold |
|---|---|---|---|
| `suppression_check_failures` | Count of any dial attempt that should have been blocked by suppression but wasn't (should always be zero) | Suppression enforcement path | **Page immediately on any non-zero value** — this is Threat Model risk #1 materializing |
| `rbac_denial_rate` | `403` responses on authenticated endpoints | API Gateway | Informational, unless spiking (could indicate a misconfigured client or a probing attacker) |
| `auth_failure_rate` | `401` responses on bearer-authenticated endpoints | API Gateway | Warn if spiking sharply |

### 2.4 Business/product metrics (PRD §9)

| Metric | Definition | Source |
|---|---|---|
| `average_lead_score` | Mean `lead_score` across completed calls in a campaign | Analysis records |
| `temperature_distribution` | Count of Hot/Warm/Cold per campaign | Analysis records |
| `conversion_rate` | Closed outcomes ÷ actionable leads (tracked downstream, per PRD §9) | External, joined against Final Output |

---

## 3. Service Level Indicators & Objectives

| SLI | SLO |
|---|---|
| Per-turn conversation latency (p95) | Within Infrastructure Architecture §5's budget, 99% of the time over a rolling 30-day window |
| Disconnect-to-snapshot latency (p95) | Within the target defined for Memory Specification §5's ordering invariant, 99.9% of the time |
| Webhook accept rate | ≥ 99.5% over a rolling 24-hour window, excluding scheduled maintenance |
| Suppression correctness | 100% — zero tolerance, tracked as an incident count rather than a percentage |

---

## 4. Alert catalog

| Alert | Severity | Condition | Routes to | Runbook reference |
|---|---|---|---|---|
| Suppression check failure | **Critical** | `suppression_check_failures` > 0 | On-call, immediate page | Security Architecture §11 (compliance incident) |
| Disconnect rate spike | Critical | `disconnect_rate` > 30% sustained 5 min | On-call, immediate page | Threat Model §11 |
| Webhook signature failure spike | Critical | `webhook_signature_failure_rate` sustained spike | On-call, immediate page | Webhook Specification §9, Security Architecture §11 |
| Disconnect-to-snapshot latency breach | High | p95 exceeds target | On-call | Memory Specification §5 |
| Per-turn latency breach | High | p95 exceeds budget, 5+ min | On-call | Infrastructure Architecture §5 |
| Reconnect success rate drop | Medium | Below 80% | On-call, next business hours if outside peak | Call State Machine §2 (Reconnecting) |
| Queue backlog growth | Medium | Trending up 30+ min | On-call, next business hours | Load Test Plan §4.1 |
| Webhook accept rate drop | Medium | Below 95% | On-call | Webhook Specification §7 |
| RBAC denial spike | Low-Medium | Sharp increase in `403`s | Security review, not necessarily paged | Security Architecture §3 |
| Retry exhaustion rate trending up | Informational | N/A | Dashboard only | PRD §9 |

---

## 5. Dashboards

| Dashboard | Audience | Contents |
|---|---|---|
| Campaign Operations | Campaign Manager, Admin | Live status counts, lead classification, per-campaign analytics (FR-3.5) |
| System Health | Engineering, on-call | All metrics in §2.1–2.3, current alert status |
| Security & Compliance | Admin, security | `suppression_check_failures`, `rbac_denial_rate`, `auth_failure_rate`, audit log summary (Security Architecture §9) |
| Business Metrics | Product, sales leadership | §2.4 metrics, trended over time |

---

## 6. Logging standards

- Every log line for a call-related event includes `contact_id` and `attempt_id`, enabling full reconstruction of a contact's journey (Security Architecture §9).
- State transitions (Call State Machine) are logged at the moment they occur, with the triggering event name.
- Secrets and PII are never logged in plaintext; log redaction rules scrub known secret-shaped and PII-shaped fields as defense-in-depth (Security Architecture §5), even for fields that shouldn't be logged in the first place.
- Webhook processing logs include `event_id`, `event_type`, and the verification result, regardless of outcome — a rejected webhook is logged with the same rigor as an accepted one (Webhook Specification §9).

---

## 7. Tracing

- Each conversational turn is traced end-to-end: utterance received → STT complete → LLM decision → TTS complete → response spoken, with a timestamp at each boundary.
- Traces are correlated to `attempt_id` so a slow or failed turn can be located within its call's broader context (transcript, memory state) during investigation.
- Disconnect events include a trace of the save sequence (Memory Specification §4's sequence diagram) so the ordering invariant can be inspected after the fact, not just asserted to hold.

---

## 8. On-call procedures

- **Critical alerts** (§4) page immediately, 24/7, regardless of time.
- **High** alerts page during business hours; page after-hours only if sustained beyond a grace period.
- **Medium/Low/Informational** alerts do not page; they surface on the System Health dashboard for review at the next working session.
- Every Critical and High alert resolution is logged with root cause and a link back to the relevant specification document (e.g. a suppression failure resolution references Security Architecture §7–§8 and Threat Model risk #1), so recurring root causes are visible over time rather than resolved identically and silently each time.

---

## 9. Traceability

| Monitoring element | Reference |
|---|---|
| Metric origin | Infrastructure Architecture §8 |
| Business metrics | PRD §9 (Success metrics) |
| Suppression-failure alert | Threat Model risk #1, Security Architecture §7–§8 |
| Disconnect-to-snapshot latency | Memory Specification §5 |
| Webhook alerts | Webhook Specification §7, §9 |
| Rate-limit-related metrics | Threat Model risk #6, Load Test Plan §4.3 |

---

*No lost conversations. More opportunities. Higher conversion.*
