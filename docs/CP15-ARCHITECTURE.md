# CP15 — Proposed Architecture (Phase B design, not yet implemented)

Everything below is a **proposal** pending owner approval. Nothing here is operational.
Labels: VERIFIED · UNVERIFIED · BLOCKED · DEFERRED · REQUIRES OWNER APPROVAL

## 1. Data ownership

| Data category | System of record | Notes |
|---|---|---|
| Call metadata/status, provider ids | PostgreSQL `call_attempt` | unchanged |
| Recording metadata + lifecycle + retention/deletion state | PostgreSQL (new additive table) | proposed `call_recording` |
| Recording audio bytes | Object storage (vendor REQUIRES OWNER APPROVAL) | never in PostgreSQL, never in Redis |
| Transcript text | PostgreSQL `conversation_message` (reuse) | no second copy; see §3 |
| Analysis | PostgreSQL `call_analysis` (unchanged) | derived data; treatment on deletion in §5 |
| Audit trail | PostgreSQL `audit_log` (reuse) | survives deletion of content |
| Backup artifacts + manifests | Backup bucket (separate credentials) | manifest also mirrored in DB for status |
| Redis | Hot state only | not authoritative for any of the above |

Four distinct copies exist and are tracked separately: **provider-hosted** (Dograh, we cannot delete — BLOCKED), **application copy** (our object store), **generated transcript** (DB), **backup copy** (ages out by backup retention). Deleting one is never reported as deleting the others.

## 2. Proposed schema (additive, one new table)

`call_recording`: `id`, `call_attempt_id` (FK, unique), `dograh_workflow_id`, `provider_run_id` (unique together with workflow id), `status`, `storage_backend`, `object_key` (random UUID-based, no PII), `content_type`, `size_bytes`, `duration_seconds`, `sha256`, `provider_artifact` (recording / user / bot), `observed_at`, `stored_at`, `last_verified_at`, `retention_until`, `deletion_requested_at`, `deleted_at`, `failure_code` (sanitized), `lease_owner`, `lease_expires_at`, `version`.

Checks: status enum; `stored` requires `sha256`, `size_bytes`, `object_key`; `deleted` requires `deleted_at`.
No signed URLs, provider URLs or keys are persisted.

States: `PENDING → WAITING_FOR_PROVIDER → INGESTING → STORED`; `RETRY_WAIT`, `UNAVAILABLE`, `FAILED`, `SKIPPED_NO_CONSENT`; `STORED → DELETION_PENDING → DELETED`. Terminal: `DELETED`, `SKIPPED_NO_CONSENT`, `UNAVAILABLE` (after bounded attempts), `FAILED`. Compare-and-set on `version` plus lease fencing, reusing the CP14B analysis pattern rather than a new queue.

Migration: additive only; downgrade refuses if any row exists in a non-`PENDING` state (it would destroy retention/deletion evidence); forward-fix documented.

## 3. Transcripts

Reuse `conversation_message`. Known gaps (no provider timestamps, no completeness/truncation flag, `fetch_transcript_lines` silently returns `[]` on failure so "missing" and "empty" are indistinguishable). Smallest fix: one nullable status/completeness column on `conversation_session` (`transcript_status`: `missing | partial | complete`). Needs a Dograh transcript JSON contract; its shape is **UNVERIFIED** (not documented in the pages read). No new transcript table is justified yet.

## 4. Ingestion (BLOCKED on owner decisions)

Trigger: webhook sets `recording_url` presence → create/upsert `call_recording` (idempotent on `(workflow_id, run_id)`). Worker re-reads the run via the existing `get_run` to obtain a fresh URL (webhook URL may have expired — expiry UNVERIFIED), checks `recording_consent == GRANTED`, streams to the store with a running SHA-256, enforces max bytes, sniffs file signature, verifies size/hash on the store, commits DB `STORED` **after** verification, then ACKs.
Redirect policy: one hop, storage-host allow-list, `X-API-Key` stripped, full IP validation again (see Audit §5.1).
Orphan handling: key is pre-registered in DB (`INGESTING`) before upload so a crash leaves a findable row; reconciler lists stale `INGESTING` rows and deletes partial objects.

## 5. Retention and deletion

Retention values are **not approved**; ship configuration with `retention_until = NULL` (keep) and **no automatic deletion**. Deletion is requested (audited, Admin-only), marks `DELETION_PENDING` (access blocked immediately), deletes our object, verifies absence, then marks `DELETED`. Transcript/analysis treatment on deletion is a policy decision (§9 Q4). Data Privacy §2 erasure cascade (contact, attempts, transcripts, recordings, analysis) and Recording-Consent §8 are the documented intents. Provider copy: not deletable via any documented API — BLOCKED; backups: age out, not individually erasable.

## 6. Access

Authenticated backend streaming (preferred: no signed URL ever leaves the server, no URL-leak surface). Admin RBAC only (single-tenant). Audit every access/deny. `Range` support DEFERRED until playback requirements are known; audio format UNVERIFIED.

## 7. Backups and restore

Logical `pg_dump -Fc` with password via `PGPASSFILE`/env (never argv), SHA-256, manifest, upload to a separate private bucket, verification download, restore into a throwaway database, relationship/row-count/checksum validation, structured report with measured duration. Object backup = versioned bucket + inventory manifest with checksums (replication alone is not a backup). **PITR: BLOCKED** — requires knowing production Postgres hosting (managed provider PITR vs self-managed WAL archiving). A `pg_dump` is described only as a logical backup.

## 8. What can be verified where

| Check | Where |
|---|---|
| Unit/contract tests with mocked Dograh + in-memory/fake object store | CI/dev with Postgres 16 + Redis (not available in the audit sandbox) |
| Migration up/down, `alembic check`, populated DB | needs real Postgres |
| Real object-store integration, encryption-at-rest, IAM, versioning | needs the approved vendor and credentials — **UNVERIFIED until then** |
| Restore drill | needs Postgres 16 with `pg_dump`/`pg_restore`; synthetic data only |
| Live Dograh recording retrieval | needs authorized test call — **UNVERIFIED/BLOCKED** |

## 9. Owner decision table (blocking; nothing below is decided)

Nothing here has been assumed. Where evidence does not exist, the item is left **UNRESOLVED**.

| # | Decision | Recommended default | Consequence of the default | Blocker / evidence gap | Exact decision needed |
|---|---|---|---|---|---|
| 1 | Object storage for recordings and backups | **Reuse the same family Dograh already uses** (S3-compatible: self-hosted MinIO or AWS S3), via a thin interface. Cloudflare R2 is also S3-compatible. Recommendation is on operational fit only; no cost data was gathered, so no cost claim is made | Needs one new dependency (S3 client) and a bucket with private ACLs, SSE, versioning, and separate backup credentials. Until chosen, only an interface plus test fake can be built; production persistence stays BLOCKED | No vendor appears anywhere in the repo, compose file, or dependencies. **UNRESOLVED** | Pick R2, S3, or MinIO, and approve the S3 client dependency (e.g. `boto3`, or a minimal SigV4 client) |
| 2 | Dograh deployment and recording hostnames | Treat both the Dograh API host and the storage redirect host as explicit allow-list entries from config; deny everything else; one redirect max; send no credentials | A mismatch fails closed (recording marked `FAILED` with a sanitized code) rather than fetching an unapproved host | `DOGRAH_API_BASE_URL` in the repo is a localhost example only. Whether you use Dograh cloud or self-host, and the storage host, are **UNRESOLVED**. Nothing was live-tested | State the real API host and the storage public host (or confirm you will provide them via config), and authorize one live test with a synthetic call |
| 3 | Consent gating | **Strict**: ingest or serve audio only if `recording_consent == GRANTED`; `NULL`, `UNCLEAR`, `DENIED`, `NOT_APPLICABLE` all → `SKIPPED_NO_CONSENT` | Safest. Some legitimately consented calls with a late or missing classification are not stored until reclassified. A skipped recording still exists at Dograh (Audit §7.6) | **Backend gating is not sufficient: see decision 9 and Audit §10.** Consent field is set by our webhook handler; timing relative to recording ingestion must be tested (ingest only after consent is final). Documented design intent supports strict gating | Confirm strict `granted`-only, and whether late reclassification to `granted` should trigger ingestion |
| 4 | Retention windows and deletion behavior | **Ship with retention OFF** (no automatic deletion). Deletion only by explicit Admin request. On recording deletion: delete our audio copy only; keep transcript and `call_analysis` unless a separate erasure request covers them | Nothing is deleted automatically. Dograh's copy is **not** deleted (no capability), so this is *not* an erasure guarantee. Backups age out per backup retention | No approved retention periods; legal basis unknown; Dograh gives no delete API. **UNRESOLVED** | (a) retention period per category or "none yet"; (b) keep / delete / anonymize transcript and `call_analysis` on recording deletion; (c) acknowledge provider-copy limitation or arrange operator-side bucket deletion |
| 5 | Production PostgreSQL hosting, backups, RPO/RTO | Defer the PITR design until hosting is known. Logical `pg_dump -Fc` backup + restore drill is buildable now. Treat RPO 24h / RTO 4h as **proposed targets to approve**, not guarantees | A periodic dump gives at best RPO = dump interval; no PITR. Measured drill duration will be reported separately from the target | Hosting (managed vs self-managed), WAL archiving, and any provider backups are unknown. PITR **BLOCKED**. Drill must run where Postgres 16 and `pg_dump` exist | Name the hosting provider/mode, and approve or change RPO 24h / RTO 4h |
| 6 | Remove `backend/dump.rdb` | **DONE** (separate hygiene commit): untracked, `*.rdb`/`*.aof`/`appendonlydir/` ignored. File was an empty snapshot (zero keys) | No history rewrite; blob remains in `bc346fe` (harmless) | None | None; informational |
| 7 | Webhook accepts non-URL `recording_url` | **DONE** (separate fix commit, see below). Non-http(s) / bare key / malformed → `None`; never fetched, trusted or persisted | Webhook can no longer be rejected because of this field. All other validation, auth, replay protection unchanged | Behaviour verified by tests against a local Postgres 16, not against a live Dograh | None; informational |
| 8 | Dograh token is a permanent bearer credential | Never persist or log recording URLs; store only `(workflow_id, run_id)` and re-fetch | Slightly more API calls per ingestion | Cannot rotate tokens via a documented API | Acknowledge; optionally ask the Dograh operator about token rotation |
| 9 | **Provider-side consent (BLOCKER, see Audit §10)** | Pick prerequisite **A** if you do not need stored recordings (`ENABLE_CALL_RECORDING_UPLOAD=false` on Dograh); otherwise B or C | Backend gating alone leaves Dograh recording and retaining audio for non-consenting calls. Not solved until verified on the deployed instance | No per-call/consent-triggered control found in source; deployed version and config unknown; Dograh cannot delete | Choose A, B, or C and supply the deployed Dograh version and recording configuration |
| 10 | **New:** `transcript_url` bare-key hazard (Audit §11) | Same advisory treatment, separately reviewed, because this URL is fetched | Avoids a rejected webhook in the no-token branch | Needs review of the fetch/fallback path | Approve or defer |

**Gate on CP15 implementation:** decision 9 must be answered before recording ingestion is built. If A is chosen, recording ingestion is out of scope and the checkpoint reduces to transcript retention, backup/restore and hygiene.

## 10. Minimal contract proposals (no implementation)

**Migration** (additive): single `call_recording` table per §2, one nullable `transcript_status` on `conversation_session` (only if decision 4 requires distinguishing missing vs empty). Single head must follow `a83d13f0c415`.

**API** (Admin-only, existing RBAC, no new role): `GET /api/v1/calls/{attempt_id}/recording` (metadata, no keys/URLs), `GET /api/v1/calls/{attempt_id}/recording/audio` (authenticated backend streaming, audited), `POST /api/v1/calls/{attempt_id}/recording/deletion` (audited, idempotent). Backup/restore remain CLI/operator procedures, not API endpoints.

**Test plan (to be written once decisions are made)**: fake Dograh responses per the verified contract (302 chain, no-token key form, missing recording), redirect to untrusted host, oversize/non-WAV payloads, consent matrix (all six values), duplicate/out-of-order webhooks, crash-after-upload orphan recovery, deletion idempotency, IDOR/anonymous denial, log redaction of token-bearing URLs. Mocked tests will be labeled mocked.
