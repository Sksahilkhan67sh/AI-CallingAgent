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

## 7. Addendum — Dograh source-code audit (second pass)

Source: public repository `dograh-hq/dograh`, shallow clone at commit `b121f43` (2026-10-08), read only. This verifies what the **open-source code does**; it does **not** prove what the owner's deployed instance does (version, configuration, and storage backend are unknown). Labels: VERIFIED FROM DOCUMENTATION · VERIFIED FROM SOURCE CODE · VERIFIED BY LIVE TEST · NOT VERIFIED · BLOCKED. **No item below is VERIFIED BY LIVE TEST.**

| Topic | Finding | Label |
|---|---|---|
| Webhook `recording_url` value | When the run has a public token (always for campaign runs and runs with webhook/QA nodes): `{BACKEND_API_ENDPOINT}/api/v1/public/download/workflow/{token}/recording`. Host is Dograh's **own API host**, not the storage host. Without a token the field is the **raw storage key** (e.g. `recordings/123.wav`), not a URL | SOURCE CODE (`api/tasks/run_integrations.py`) |
| Public download authentication | **None.** Lookup is by token equality only; no expiry, no revocation field read. The token is a permanent bearer credential for that run's audio and transcript | SOURCE CODE (`api/routes/public_download.py`, `workflow_run_client.py`) |
| Redirect behavior | 302 to a freshly generated signed storage URL. Host = `MINIO_PUBLIC_ENDPOINT` (self-hosted MinIO) or the S3 endpoint/bucket host | SOURCE CODE + DOCS |
| Signed URL expiry | 3600 s, generated per request | SOURCE CODE |
| Audio format | WAV (`recordings/{run}.wav` mixed; `/user.wav`, `/bot.wav`). Per-track metadata stores `format: "wav"` | SOURCE CODE (`workflow_run_artifacts.py`) |
| Availability timing | Artifacts are uploaded **before** the completion job is enqueued, so recordings should exist when the webhook fires. An upload failure is only logged; the run then has no recording and the webhook field is `null`/absent | SOURCE CODE. Not live-tested |
| Per-artifact failure | Mixed/user/bot/transcript uploads are independent; one can fail while others succeed | SOURCE CODE |
| Storage backends | S3 (incl. custom endpoint) and MinIO; local/null filesystems exist | SOURCE CODE |
| Recording/transcript deletion | **No delete method** in the storage abstraction (`api/services/filesystem/base.py`), and no deletion route found | SOURCE CODE → deletion BLOCKED |
| Retention for recordings | **None found.** Only `LOG_RETENTION` (log files). Recordings persist until the operator manages the bucket | SOURCE CODE |
| Recording lifecycle webhooks | None found; only the post-run webhook node | SOURCE + DOCS |
| Run id as idempotency key | `workflow_run_id` is a DB integer; unique within one Dograh database. Scope by `(dograh_workflow_id, run_id)` | SOURCE CODE |

### Consequences for design

1. **No API key is needed (or should be sent) to download.** The documented public route is unauthenticated. The downloader must send **no credentials**, which removes the key-forwarding-on-redirect risk but leaves the token itself as a secret: the webhook URL contains it and must never be logged or stored.
2. **Two hosts need allow-listing**: Dograh's API host (the URL in the webhook, = `DOGRAH_API_BASE_URL` host if same deployment — UNVERIFIED) and the storage host in the redirect. The storage host cannot be known from the repo. BLOCKED on owner decision.
3. **Latent contract bug in our webhook schema (VERIFIED FROM SOURCE CODE, behavior NOT VERIFIED):** `DograhWebhookPayload` rejects any `recording_url` not starting with `http(s)://`. In Dograh's no-token branch the value is a bare storage key, which would fail validation and could cause the **whole webhook** to be rejected. Campaign runs always get a token, so the normal path is unaffected. **FIXED** in commit `fix(webhook): make recording_url advisory…`: non-URL values normalise to `None`; covered by tests (see git log).
4. **The token is permanent.** Any copy of a recording URL in our logs, DB, or error reports is a long-lived credential. Persist only `(workflow_id, run_id)` and re-fetch via `get_run` when needed (this returns the token over the authenticated API).
5. **Dograh copy cannot be erased by us or by API.** After CP15 stores an application copy and the owner deletes it, the original WAV remains in Dograh storage and remains reachable by anyone holding the token. Any "deleted" status must say *application copy deleted; provider copy retained* unless the owner administers Dograh's bucket directly.
6. **Non-granted consent**: Dograh records according to its own workflow configuration, independent of our `recording_consent` field. A recording can therefore exist at Dograh for a call whose consent we classify as denied/unclear. CP15 can refuse to copy it, but cannot delete the provider copy via API. This is a compliance risk to raise with the owner, not something CP15 can solve in code.

## 8. `backend/dump.rdb` (inspected without exposing content)

| Item | Result |
|---|---|
| Tracked in git | Yes, since commit `bc346fe` (2026-10-08, "checkpoint-13: harden dograh provider handling"); only that commit touches it |
| Ignored by `.gitignore` | No (`*.rdb` rule absent) |
| Size / format | 89 bytes, RDB version `0010` header |
| Content | Parsed structurally: 5 header metadata fields (`redis-ver`, `redis-bits`, `ctime`, `used-mem`, `aof-base`), then the end-of-file marker — **zero keys, zero databases** |
| Sensitive data | None found (no keys exist). Values of the header fields were not printed |
| Action | **Done in a separate hygiene commit (`chore: untrack empty Redis snapshot…`):** file removed from the tree, `*.rdb`, `*.aof`, `appendonlydir/` added to `.gitignore`. No history rewrite; the blob remains in `bc346fe` (harmless: zero keys). Nothing in the repo referenced the file |

## 9. Verification status of this checkpoint so far

| Item | Status |
|---|---|
| Dograh docs reviewed | VERIFIED FROM DOCUMENTATION (see §4) |
| Dograh source reviewed | VERIFIED FROM SOURCE CODE (commit `b121f43`) |
| Any live Dograh call/download | NOT VERIFIED — no authorized credentials/instance used |
| Tests, migrations, `alembic check`, build, restore drill | **NOT RUN** — no code changed; sandbox has no Postgres/Redis/Docker |

## 10. Provider-side recording-consent blocker (BLOCKER, NOT SOLVED)

**Statement.** Backend ingestion gating (store/serve audio only if `recording_consent == GRANTED`) is necessary but **not sufficient**. It controls only our *copy*. It cannot stop Dograh from recording, and it cannot delete Dograh's audio. `Recording-Consent.md` requires that audio is not retained without consent; our backend cannot satisfy that on its own.

**Evidence (all VERIFIED FROM SOURCE CODE, `dograh-hq/dograh` @ `b121f43`; NOT VERIFIED against the owner's deployed version or configuration):**

| Fact | Source |
|---|---|
| Audio capture starts in the `on_client_connected` handler (`audio_buffer.start_recording()`), i.e. when the call media connects, **before** the greeting or any consent question, and with no consent or per-workflow condition in that path | `api/services/pipecat/event_handlers.py` |
| At call end, mixed/user/bot WAV are uploaded to the configured store and `recording_url` + `extra.recordings` are written — unconditionally, unless the global flag below is off | `event_handlers.py`, `api/services/workflow_run_artifacts.py` |
| The **only** switch found is the instance-wide environment variable `ENABLE_CALL_RECORDING_UPLOAD` (default `true`). When `false`, audio is still buffered in memory during the call, but it is **not uploaded**, and no `recording_url` or recording metadata is produced | `api/constants.py`, `event_handlers.py` |
| No per-call, per-campaign, per-workflow, or consent-triggered recording control was found | grep of the source; absence of evidence, not proof for other versions |
| No delete method exists in the storage abstraction and no retention for recordings | see §7 |

**Consequences.**
1. For every connected call, audio of the opening (including the agent's consent question and the caller's answer) is captured by Dograh whenever upload is enabled, regardless of the eventual consent outcome.
2. A recording can therefore exist at Dograh for a call we classify `DENIED`, `UNCLEAR`, or `NULL`. CP15 can decline to copy it. It cannot erase it.
3. Dograh's public download token is permanent (§7), so that retained audio stays reachable by anyone who holds the token.

**Exact prerequisite to guarantee "no recording before valid consent"** (one of the following must be true and then *verified against the deployed Dograh configuration*; none is verified today):
- **(A) Provider recording persistence disabled:** the Dograh instance runs with `ENABLE_CALL_RECORDING_UPLOAD=false`. Effect: no persistent recordings exist, so CP15 recording ingestion has nothing to ingest (CP15 would then be transcript/analysis/backup only). Audio is still held in process memory during the call (Dograh's own comment says integrations consume it).
- **(B) Consent-triggered recording start:** the Dograh version/configuration supports starting capture only after consent is given. **No such capability was found in source.** Would require a Dograh change or a documented feature; NOT VERIFIED to exist.
- **(C) Legal determination:** the owner/legal counsel decides, in writing, that capturing the consent exchange itself and deleting non-consented audio afterward is acceptable for the jurisdictions called. This is a legal decision, not an engineering one, and **deletion at Dograh is BLOCKED** so "delete afterward" would need operator-level bucket deletion.

**What would count as verified** (not yet done): a documented check of the deployed instance's `ENABLE_CALL_RECORDING_UPLOAD` value (or its replacement), and an authorized synthetic call showing the resulting run has the expected recording state (none for A; none until consent for B). Until then CP15 documentation, runbooks, and PR text must say *consent gating is partial: application copy only*.

**Decision needed from the owner:** choose A, B, or C (or a combination), and provide the deployed Dograh version and its recording configuration so it can be verified.

## 11. Additional finding: `transcript_url` has the same bare-key hazard (NOT FIXED, decision needed)

In Dograh's no-token branch, `transcript_url` is also rendered as a bare storage key (`transcripts/123.txt`, `api/tasks/run_integrations.py`). Our schema still requires `http(s)` for `transcript_url`, so such a webhook would still be rejected with 422 (VERIFIED FROM SOURCE for Dograh's value; our rejection VERIFIED BY TEST). It was deliberately **not** changed because the transcript URL **is fetched** (SSRF-guarded), so loosening it has different trade-offs. Campaign runs always receive a token, so the normal path is unaffected. Recommended: same advisory normalisation (non-URL → "no transcript URL", fall back to the existing `get_run` path), with its own review.
