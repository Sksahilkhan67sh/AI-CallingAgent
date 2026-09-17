# AI Calling Agent — Load Test Plan

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Test Plan v1.0 (§10) · Infrastructure Architecture v1.0 · Threat Model v1.0 · FRS v1.0 · Webhook Specification v1.0

**Purpose:** Test Plan §10 lists the load/chaos test cases; this document is the full plan behind them — objectives, load profiles, environment, metrics, and explicit pass/fail thresholds. It's also where Threat Model risk #2 (outage-shaped webhook flood) gets a concrete, executable design rather than a described gap.

---

## 1. Objectives

- Confirm the system meets the PRD's scale requirement (100,000+ contacts per campaign) without degrading dispatch rate or conversation quality.
- Confirm the Conversation Orchestrator sustains target concurrent-call volume within the conversational latency budget (Infrastructure Architecture §5).
- Confirm the webhook ingestion path survives a legitimate disconnect flood shaped like a real telephony outage, without dropping or excessively delaying events — closing Threat Model risk #2.
- Confirm retry-burst traffic doesn't starve fresh dials.
- Establish baseline capacity numbers to inform the Infrastructure Architecture's scaling configuration, not just pass/fail this test cycle.

---

## 2. Scope

**In scope:** Calling Queue throughput, Conversation Orchestrator concurrency, webhook ingestion under burst/flood conditions, Retry Policy Engine behavior under simultaneous-eligibility bursts, Primary DB and Object Storage write throughput under load.

**Out of scope:** STT/LLM/TTS provider-side performance (treated as external dependencies with their own SLAs, monitored but not load-tested here), UI/dashboard rendering performance under concurrent admin users.

---

## 3. Test environment

- Run in Staging (Infrastructure Architecture §9), sized to match production capacity as closely as feasible — a scaled-down environment invalidates absolute throughput numbers, though relative degradation patterns are still useful.
- Sandboxed telephony/STT/LLM/TTS provider credentials, with the telephony sandbox configured to support scripted connect/disconnect behavior on demand (needed for scenarios in §4).
- Synthetic contact data only (Test Plan §11) — no production PII in load-test runs.

---

## 4. Load scenarios

### 4.1 Sustained campaign dispatch (Scale)
**Setup:** a campaign of 100,000+ synthetic contacts, configured call rate at the documented maximum.
**Run:** dispatch continuously until the campaign completes or a fixed test window elapses (whichever is shorter, per test cycle time budget).
**Measure:** dispatch rate over time, queue backlog size over time, error rate on dial attempts.
**Pass criteria:** dispatch rate stays within 5% of the configured target throughout; queue backlog does not grow unbounded (a temporary backlog that later drains is acceptable; one that trends upward for the full run is not).

### 4.2 Concurrent live-call ramp (Concurrency)
**Setup:** ramp simulated concurrent connected calls from 0 to a target concurrency level (set per current capacity planning; document the actual number used and its source).
**Run:** hold at target concurrency for a sustained period once reached.
**Measure:** per-turn latency (Listen → Understand → Respond round trip) at each concurrency step; Orchestrator pod scaling behavior; error/timeout rate.
**Pass criteria:** per-turn latency at target concurrency stays within the budget defined in Infrastructure Architecture §5; no unbounded latency growth as concurrency increases toward the target.

### 4.3 Outage-shaped webhook flood (Chaos — closes Threat Model risk #2)
**Setup:** construct a synthetic `call.disconnected` event stream shaped to resemble a real provider-side outage — a sharp spike in disconnects across many concurrently active calls within a short window, all validly signed (this is legitimate traffic, not an attack, and must be distinguishable as such by the system's own behavior, not by the test cheating with a special flag).
**Run:** fire the event stream at the webhook ingestion endpoint while the currently-configured rate limiting and anomaly detection (Security Architecture §6) are active, unmodified for the test.
**Measure:** percentage of events accepted vs. throttled/dropped; latency from event receipt to snapshot durably committed (Memory Specification §5); whether any accepted event's processing violates the disconnect-save-before-retry-decision ordering invariant under load.
**Pass criteria:** at least 99% of the legitimate flood is processed within the latency budget from §4.2 (disconnect handling is on the same critical path); zero ordering-invariant violations; any throttling that does occur must not be indistinguishable from data loss — a throttled event should be queued and eventually processed, not silently discarded.
**If this fails:** per Threat Model risk #2, this scenario failing means rate limiting is tuned incorrectly for outage-shaped traffic and must be retuned (e.g. traffic-shape-aware limits, or prioritizing `call.disconnected` above other webhook types) before the risk can be marked mitigated.

### 4.4 Retry burst (Scheduling)
**Setup:** schedule a large number of contacts to become simultaneously eligible for retry (e.g. many 30-second retries landing in the same window, as would happen after a shared upstream failure).
**Run:** let the burst fire and observe queue processing alongside a steady stream of fresh (never-yet-dialed) contacts also present in the queue.
**Measure:** dispatch latency for fresh contacts during the burst window, vs. outside it.
**Pass criteria:** fresh-dial dispatch latency during the burst does not increase by more than an agreed factor (document the threshold used) relative to baseline — the retry burst must not starve new dials entirely.

### 4.5 Data layer write throughput (Supporting)
**Setup:** drive Primary DB and Object Storage write load consistent with the concurrency level in §4.2 (transcripts, recordings, analysis writes per completed/disconnected call).
**Measure:** write latency and error rate at the data layer.
**Pass criteria:** write latency does not become the bottleneck limiting the concurrency achieved in §4.2 — i.e. the Conversation Orchestrator's own latency budget, not a data-layer queue, is the limiting factor.

---

## 5. Metrics and instrumentation

| Metric | Source | Relevant scenario |
|---|---|---|
| Dispatch rate (calls/sec) | Calling Queue metrics | §4.1 |
| Queue backlog size | Calling Queue metrics | §4.1, §4.4 |
| Per-turn latency (p50/p95/p99) | Conversation Orchestrator metrics | §4.2 |
| Orchestrator pod count over time | Infrastructure autoscaling metrics | §4.2 |
| Webhook accept/throttle/drop rate | Webhook ingestion metrics | §4.3 |
| Time from disconnect receipt to durable snapshot | Recovery Layer / State Saver metrics | §4.3 |
| Fresh-dial dispatch latency | Calling Queue metrics, segmented by contact type | §4.4 |
| DB/object storage write latency | Data layer metrics | §4.5 |

These align with the metrics already specified as first-class in Infrastructure Architecture §8 — this test plan's job is to exercise them under controlled load, not introduce new ones.

---

## 6. Pass/fail summary

| Scenario | Pass threshold | Status |
|---|---|---|
| 4.1 Sustained dispatch | Rate within 5% of target; no unbounded backlog growth | Not yet run |
| 4.2 Concurrent-call latency | Per-turn latency within budget at target concurrency | Not yet run |
| 4.3 Outage-shaped webhook flood | ≥99% processed within budget; zero ordering violations; no silent drops | Not yet run — **closes Threat Model risk #2 when passing** |
| 4.4 Retry burst | Fresh-dial latency degradation within agreed factor | Not yet run |
| 4.5 Data layer throughput | Not the binding constraint on §4.2's result | Not yet run |

A scenario that fails is a release-blocking finding only for §4.3 (tied to a named Threat Model risk); the others feed capacity-planning decisions and are prioritized by severity of the gap found, per standard defect management (Test Plan §12).

---

## 7. Reporting

Each run produces: the metrics in §5 over time, pass/fail against §6, and — specifically for §4.3 — an explicit statement of whether Threat Model risk #2 can be marked mitigated or remains open with a described gap, consistent with the Threat Model's own practice of not overstating mitigation status.

---

## 8. Traceability

| Element | Reference |
|---|---|
| Origin of this plan | Test Plan §10 |
| Scale requirement | PRD §8 (Non-functional requirements) |
| Latency budget | Infrastructure Architecture §5 |
| Ordering invariant under test in §4.3 | Memory Specification §5 |
| Risk being closed | Threat Model risk #2 |

---

*No lost conversations. More opportunities. Higher conversion.*
