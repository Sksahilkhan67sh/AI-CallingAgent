# CP15 — Data Audit (Phase A)

Status labels: **VERIFIED** (read in code/docs this session) · **UNVERIFIED** · **BLOCKED** · **DEFERRED** · **REQUIRES OWNER APPROVAL**

This is an audit only. No schema, storage, or provider code has been written.

## 1. Git baseline

| Item | Result |
|---|---|
| Remote `develop` HEAD | `5d0b21e33c20f7d21909ae6de413e829c19ed922` — VERIFIED (fetched this session) |
| CP14B merged | VERIFIED: PR #48 merge commit `5d0b21e`; migration `a83d13f0c415` present |
| Alembic heads | Migration files read; `alembic heads` NOT RUN (no Python deps / Postgres in the audit sandbox) |
| Feature branch | `feature/checkpoint-15-data-recording-backup` created from `5d0b21e` |

## 2. Existing data model (VERIFIED by reading models)

| Entity | Relevant facts |
|---|---|
| `call_attempt` | `provider_call_id` (Dograh `workflow_run_id`, partial unique index), `dograh_workflow_id`, `recording_consent` (nullable enum), `duration_seconds`, `started_at`/`ended_at` |
| `conversation_session` | 1:1 with attempt (`call_attempt_id` unique) |
| `conversation_message` | `(session_id, sequence)` unique; `role`, `content` (Text). No timestamp-from-provider, speaker label beyond `role`, language, or completeness flag |
| `call_event` | per-attempt JSONB event log |
| `call_analysis` | CP06/CP14B pipeline; lease/fencing/retry fields |
| `audit_log` | `actor`, `action`, `entity_type`, `entity_id`, JSONB `metadata` — extendable without a new system |
| `processed_event` | webhook idempotency by `event_id` |
| Recording / object storage / retention / backup | **None exist.** No `recording` table, no storage client, no boto/S3 dependency, no backup scripts, no retention settings |

Note: there is no tenant/owner column (single-tenant, CP11 Option A). Per-user authorization for recordings reduces to the existing admin RBAC (Admin / Operator).

## 3. What the code already does with recordings/transcripts

- `DograhWebhookPayload` accepts `recording_url` and `transcript_url` (≤2048 chars, `http(s)` only) — VERIFIED.
- `recording_url` is **parsed and then ignored** (no consumer anywhere in `app/`) — VERIFIED by grep.
- `transcript_url` is fetched by `transcript_fetch.py`, an SSRF-hardened downloader (DNS pinning, no redirects, size/line caps, content-type allow-list, no URL logging) — VERIFIED. Its policy is "redirects are never followed" and "JSON content types only", so it **cannot be reused as-is for audio** (see §5).
- Dograh API client already calls `GET /api/v1/workflow/{workflow_id}/runs/{run_id}` with `X-API-Key` (CP14B `get_run`) — VERIFIED.

## 4. Dograh capability matrix

Sources: docs.dograh.com pages "Calls & Runs", "Webhook Payloads", "Retrieve Agent Run Details", "Download Recordings and Transcripts", and `llms.txt` (full page index) — fetched this session. No live call or live API request was made.

| Capability | Status | Evidence / limitation |
|---|---|---|
| Recording exists per run | VERIFIED (docs) | "Calls & Runs": every run has transcript and recording |
| `recording_url` in webhook | VERIFIED (docs) | Webhook variable `{{recording_url}}` |
| `recording_url`, `user_recording_url`, `bot_recording_url` on run record | VERIFIED (docs) | `GET /api/v1/workflow/{workflow_id}/runs/{run_id}` response schema; auth via `X-API-Key` or `Authorization` header |
| Public time-limited download | VERIFIED (docs) | `GET /api/v1/public/download/workflow/{token}/{artifact_type}`; `artifact_type` ∈ recording, transcript, user_recording, bot_recording; **302 redirect to a signed URL**; token from run details (`public_access_token`) |
| Run id as stable idempotency key | VERIFIED (docs) | `workflow_run_id` integer; uniqueness scope across workflows/orgs not documented → treat as `(dograh_workflow_id, run_id)` |
| Audio format / content type | **UNVERIFIED** | Not documented. Must be sniffed and allow-listed, not assumed |
| Expiry of `recording_url` | **UNVERIFIED** | Not documented. Download-token expiry is "time-limited" with no duration given |
| Auth required on `recording_url` | **UNVERIFIED** | Not documented; treat as bearer credential |
| Recording availability delay after call end | **UNVERIFIED** | Not documented; webhook fires "after run completes" but readiness of audio is not stated |
| Recording deletion API | **BLOCKED / not documented** | No delete endpoint in the full `llms.txt` index |
| Transcript deletion API | **BLOCKED / not documented** | Same |
| Provider retention configuration | **UNVERIFIED** | No retention page in docs index |
| Recording lifecycle webhooks | **not documented** | Only the post-run webhook node exists |
| Provider-side storage location/encryption | **UNVERIFIED** | Self-hosted Dograh uses its own object store; configuration is the operator's, not documented as an API |

Consequence: **retrieval is documented; deletion and retention at Dograh are not.** CP15 can copy a recording into application storage, but deleting our copy does **not** delete Dograh's. This must never be reported as erasure.

## 5. Findings that change the design

1. **Redirect conflict.** The documented download path redirects to a signed URL on a storage host that is not Dograh's. The existing fetcher forbids redirects and trusts only Dograh's host. Recording ingestion therefore needs a *new*, narrowly-scoped policy: follow at most one redirect, only to an explicitly configured storage-host allow-list, strip `X-API-Key` on the redirect hop, re-run the full IP validation on the target. REQUIRES OWNER APPROVAL of the allow-list approach, and the actual storage host for the deployment is UNVERIFIED.
2. **Consent gating is a correctness requirement.** `Recording-Consent.md` §6/§8 says audio must not be retained unless `recording_consent = granted`, and an unconsented recording "must be deleted, not merely marked unused." Ingestion must therefore store audio **only** when `call_attempt.recording_consent == GRANTED`; `NULL`, `UNCLEAR`, `DENIED`, `NOT_APPLICABLE` → do not ingest. A recording found after a `DENIED` result is a compliance incident path that needs deletion of our copy (and a flagged, unresolvable-by-us provider copy).
3. **No object store is approved.** Design docs (Infrastructure Architecture §4) say "Object Storage" abstractly; no vendor is chosen anywhere in repo, compose file, or dependencies. Adding S3/R2/MinIO is a new infrastructure component and a new dependency (`boto3` or `httpx` + SigV4). REQUIRES OWNER APPROVAL.
4. **Local dev/CI has no Postgres/Docker in the audit sandbox.** Migration, `alembic check`, populated-DB, and restore-drill verification require a real Postgres 16 (compose uses `postgres:16`). Not runnable here. Anything claimed must come from a CI/dev run.
5. **Existing design stub:** `Database-Design.md §2.9 recording_ref` (`attempt_id` unique, `storage_path`, `duration_seconds`) is the original plan; it lacks status, checksum, size, retention and deletion state, so it is insufficient as-is for CP15.
6. **Repository hygiene.** `backend/dump.rdb` (89 bytes, an empty Redis snapshot) is tracked in git since commit `bc346fe` (CP13). It contains no data but violates the "no Redis snapshots" rule. Recommend `git rm --cached` + `.gitignore` in a separate cleanup; not touched here.
7. **Docker Compose defaults** (`postgres/postgres`, ports published) are dev-only. Backup design must not reuse them.

## 6. Storage / database deployment facts

| Item | Status |
|---|---|
| Object store (S3/R2/MinIO) | None present — **BLOCKED** pending owner choice |
| PostgreSQL | `postgres:16` in compose; production hosting, WAL archiving, managed backups: **UNVERIFIED** (nothing in repo) |
| Existing backup/restore scripts or IaC | None found |
| PITR | **BLOCKED** until production Postgres hosting is known; a `pg_dump` is not PITR |
| Alerting integration | None found; logs only |

## 7. Owner decisions required before implementation

See `docs/CP15-ARCHITECTURE.md` §9.
