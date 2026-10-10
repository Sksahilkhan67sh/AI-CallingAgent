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

## 9. Owner decisions needed (blocking)

1. **Object store vendor** for recordings and backups (S3 / Cloudflare R2 / MinIO self-hosted / other) and whether adding an S3-compatible client dependency is approved. Without this, I can build the interface and a test fake only; production persistence stays BLOCKED.
2. **Redirect allow-list**: where does your Dograh deployment serve recordings from (Dograh cloud vs self-hosted MinIO/S3 host)? Needed for the one-hop redirect rule.
3. **Consent gating**: confirm audio is ingested **only** when `recording_consent = granted` (my recommendation, matching `Recording-Consent.md`).
4. **On deletion of a recording**: keep, delete, or anonymize the transcript and `call_analysis`? And are retention windows decided, or ship with retention off?
5. **Production Postgres hosting** (managed provider vs self-managed) to decide PITR path, and RPO/RTO targets (proposal: RPO 24h / RTO 4h as options to approve, not claims).
6. Whether CP15 may include a small cleanup commit removing tracked `backend/dump.rdb`.
