# AI Calling Agent — Environment Configuration

Complete outbound calling workflow, with disconnect recovery · v1.0 · Status: Production Ready

**Related documents:** Infrastructure Architecture v1.0 · Security Architecture v1.0 · Deployment Guide v1.0 · Database Design v1.0 · Webhook Specification v1.0 · API Specification v1.0

**Purpose:** a concrete reference for every configuration value the system needs to run, per environment — where it lives, whether it's a secret, and what changes between Development, Staging, and Production. Deployment Guide §2 references this document's checklist items.

---

## 1. Two kinds of configuration — read this first

This system has two entirely different configuration layers, and conflating them is a common source of deployment mistakes:

| Layer | Examples | Where it lives | Changed by |
|---|---|---|---|
| **Infrastructure config** (this document) | Database connection strings, provider API keys, service URLs, log levels | Environment variables / secrets manager, set at deploy time | A deployment (Deployment Guide §6) |
| **Business config** (NOT this document) | Retry policy (max retries, spacing, calling window), agent persona/script | Primary DB (`retry_policy`, `agent_config` tables), set via Admin Dashboard | An Admin, at runtime, no deployment (System Architecture, Database Design §2.2–2.3) |

**Rule of thumb:** if a campaign manager or admin should be able to change it without waiting for engineering, it belongs in Business config and does **not** appear as an environment variable. Retry policy defaults shown as examples in earlier documents (max retries = 2, calling window 10 AM–6 PM) are **database row defaults for a new campaign**, not environment variables — do not create an env var for these.

---

## 2. Environment variable reference

### 2.1 Database & data layer

| Variable | Description | Secret? | Dev | Staging | Production |
|---|---|---|---|---|---|
| `PRIMARY_DB_URL` | Connection string for the Primary DB (Database Design) | Yes | Local/dev DB | Staging DB | Production DB, read/write replica-aware |
| `PRIMARY_DB_POOL_SIZE` | Connection pool size per service instance | No | `5` | `20` | `50` (tune per Infrastructure Architecture §6) |
| `OBJECT_STORAGE_BUCKET` | Bucket/container for recordings (Database Design §2.9) | No | `dev-recordings` | `staging-recordings` | `prod-recordings` |
| `OBJECT_STORAGE_CREDENTIALS` | Access credentials for object storage | Yes | Sandbox | Sandbox | Production, encrypted at rest per Security Architecture §4 |
| `MEMORY_STORE_URL` | Connection string for the working-memory in-memory store (Memory Specification, Infrastructure Architecture §4) | Yes | Local | Staging cluster | Production cluster |
| `ANALYTICS_STORE_URL` | Connection string for the denormalized Final Output / dashboard read-model | Yes | Local | Staging | Production |

### 2.2 Telephony provider

| Variable | Description | Secret? | Notes |
|---|---|---|---|
| `TELEPHONY_PROVIDER_API_KEY` | Provider credential for placing calls | Yes | Sandbox key in Dev/Staging, live key in Production |
| `TELEPHONY_PROVIDER_BASE_URL` | Provider API base URL | No | Differs between sandbox and production per provider |
| `TELEPHONY_WEBHOOK_SIGNING_SECRET` | Verifies `X-Provider-Signature` (Webhook Specification §4) | Yes | Rotatable; supports dual-secret transition (Security Architecture §5) |

### 2.3 Speech/AI providers

| Variable | Description | Secret? | Notes |
|---|---|---|---|
| `STT_PROVIDER_API_KEY` | Speech-to-text credential | Yes | |
| `STT_PROVIDER_REGION` | Region for latency-sensitive placement (Infrastructure Architecture §5) | No | Should match Orchestrator deployment region |
| `LLM_PROVIDER_API_KEY` | Language model credential | Yes | |
| `LLM_PROVIDER_MODEL_ID` | Which model version to call | No | Distinct from `prompt_template_version` (Database Design §2.3), which is business config |
| `TTS_PROVIDER_API_KEY` | Text-to-speech credential | Yes | |
| `TTS_PROVIDER_REGION` | Region for latency-sensitive placement | No | Should match Orchestrator deployment region |

### 2.4 API / authentication

| Variable | Description | Secret? | Notes |
|---|---|---|---|
| `JWT_SIGNING_KEY` | Signs/verifies bearer tokens (Security Architecture §2) | Yes | Rotate per organizational key-rotation policy |
| `JWT_EXPIRY_SECONDS` | Token lifetime | No | Shorter in Production than Dev is acceptable |
| `API_RATE_LIMIT_PER_MINUTE` | Concrete threshold closing Threat Model risk #6 | No | **Must be explicitly set — see §4 below** |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` | Threshold for webhook ingestion, informed by Load Test Plan §4.3 | No | Set from load-test results, not guessed |

### 2.5 Service-to-service

| Variable | Description | Secret? | Notes |
|---|---|---|---|
| `SERVICE_MESH_TLS_CERT` | Certificate for mutual TLS between internal services (Security Architecture §6) | Yes | |
| `EVENT_BUS_URL` | Connection for the disconnect/reconnect event bus (Infrastructure Architecture §1) | Yes | |
| `CALL_QUEUE_BROKER_URL` | Message broker connection for the Calling Queue (Infrastructure Architecture §3) | Yes | Must support delayed/scheduled delivery for retry spacing |

### 2.6 Observability

| Variable | Description | Secret? | Notes |
|---|---|---|---|
| `LOG_LEVEL` | Logging verbosity | No | `debug` in Dev, `info` in Staging/Production |
| `METRICS_ENDPOINT` | Where metrics (Infrastructure Architecture §8) are shipped | No | |
| `ALERTING_WEBHOOK_URL` | Where alert conditions (disconnect-rate spike, etc.) notify operations | Yes | Production only, typically |

### 2.7 Feature flags

| Variable | Description | Default |
|---|---|---|
| `ENABLE_RECORDING_CONSENT_DISCLOSURE` | Global default for whether the recording-consent disclosure flow (Recording Consent §3) is active for new campaigns | `true` — disabling requires explicit, logged Admin action per Recording Consent §7 / Security Architecture principle of conservative defaults |
| `ENABLE_OUTPUT_SIDE_GUARDRAIL_VALIDATION` | Whether the output-side guardrail check (Threat Model risk #3 mitigation) is active | `false` until implemented and validated in Staging |

---

## 3. Per-environment summary

| Aspect | Development | Staging | Production |
|---|---|---|---|
| Telephony/STT/LLM/TTS | Mocked or sandboxed | Sandboxed, scriptable (Load Test Plan §3) | Live provider credentials |
| Database | Local or shared dev instance | Staging instance, production-shaped synthetic data | Production instance, encrypted, backed up |
| Secrets source | Local `.env` acceptable for non-sensitive dev-only values; real secrets still via secrets manager | Secrets manager | Secrets manager, stricter access control |
| Rate limits | Relaxed or disabled | Set to match Production for realistic load testing | Set from Load Test Plan results |
| Monitoring/alerting | Minimal | Full, but alerts not paged to on-call | Full, paged to on-call |

---

## 4. Values that must not be guessed

Two settings are explicitly called out in the Threat Model as needing a deliberate decision rather than an arbitrary default:

- **`API_RATE_LIMIT_PER_MINUTE` and `WEBHOOK_RATE_LIMIT_PER_MINUTE`** (Threat Model risk #6): set from the Load Test Plan's §4.3 outage-shaped flood results, not from an assumed "reasonable" number. Document the source run/date when set.
- **Field-level encryption scope** (Threat Model risk #7): not an environment variable, but a confirmed list of which columns (Database Design) have field-level encryption applied, maintained alongside this document rather than left implicit.

---

## 5. What is deliberately absent from this document

- Retry policy values (max retries, spacing, calling window) — see §1, these are Business config in the `retry_policy` table.
- Agent persona, script skeleton, required entity fields — Business config in `agent_config`.
- Suppression list contents — data, not configuration.

If a value from these categories appears to need an environment variable to "make it work," that's a sign of a design regression against the "configuration without deployment" principle (System Architecture) and should be corrected rather than accommodated.

---

## 6. Traceability

| Config category | Reference |
|---|---|
| Business vs. infrastructure config distinction | System Architecture (configuration without deployment), Database Design §2.2–2.3 |
| Secrets handling | Security Architecture §5 |
| Rate limit sourcing | Threat Model risk #6, Load Test Plan §4.3 |
| Deployment-time checklist usage | Deployment Guide §2 |

---

*No lost conversations. More opportunities. Higher conversion.*
