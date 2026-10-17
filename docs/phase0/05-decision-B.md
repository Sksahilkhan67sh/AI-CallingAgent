# P0-5 Decision B: retention periods

Labels: [READ] = read in code/docs (file:line). Paths relative to repo root.
This file proposes NO retention values where law is involved ("needs legal input"). Only technical minimums are filled.

## 1. Global rules [READ]
- Setting type `int | None`; None = unset, 0 = keep forever, negative is rejected: `backend/app/core/config.py:550-555, 574-575`.
- In production every one of the six must be set explicitly or the service refuses to start: `config.py:580-585`. In development/test/staging unset means keep forever: `config.py:546-548`.
- `suppression` has no setting and is never purged: `config.py:547-548`; `retention.py:11-13` (a guard test fails if purge code mentions it); `docs/DATA-INVENTORY.md:28`.
- Legal hold (campaign or contact) excludes covered rows from every class: `retention.py:9`; `docs/CHECKPOINT-15A-NOTES.md:53`.
- Runs are dry-run unless `apply=True`, batched, advisory-locked: `retention.py:3-7`. Batch size 1-10000 (`config.py:556, 576-577`), runtime budget >= 1 s (`config.py:557, 578-579`).
- Backups are not edited when a period passes; old backups keep the data until they expire: `docs/DATA-INVENTORY.md:53`.
- Dograh-side audio and transcripts cannot be deleted from this repo (no documented API): `docs/CHECKPOINT-15A-NOTES.md:102`; `docs/DATA-INVENTORY.md:37`. Retention here does not cover them.

## 2. Setting-by-setting table
| Setting | What is deleted / anonymised | Depends on | Hard constraints | What breaks if too short | Proposed value | Source |
|---|---|---|---|---|---|---|
| `RETENTION_TRANSCRIPT_DAYS` | Deletes `conversation_message` rows older than the cutoff; scrubs `working_memory_snapshot` content (entities, objections, last utterance). Attempt state, duration, scores, enums stay. | `created_at` / `snapshotted_at`; legal hold on the contact; not applied while the attempt's analysis is PENDING/PROCESSING/RETRY_WAIT | 0 = forever; must be set in production | A stuck analysis blocks its own transcript purge (`CHECKPOINT-15A-NOTES.md:106`); transcripts are the input to analysis, so too short means analysis cannot be re-run. The Dograh webhook writes into the same table (note 3) | needs legal input | `retention.py:123-170, 275-276`; `DATA-INVENTORY.md:29`; `CHECKPOINT-15A-NOTES.md:96` |
| `RETENTION_ANALYSIS_DAYS` | Nulls `call_analysis` free text and evidence (summary, feedback, error text, key facts, objections, customer needs). Scores, enums, provenance stay. | `created_at`; only terminal statuses; legal hold on the contact | 0 = forever | Loses qualitative evidence for QA or dispute review; aggregate scores remain | needs legal input | `retention.py:172-203, 277`; `DATA-INVENTORY.md:30` |
| `RETENTION_CONTACT_DAYS` | Replaces phone fields with a unique non-reversible placeholder, sets `purged_at`; keeps status and counters | Counted from `campaign.closed_at`; only COMPLETED campaigns; a campaign with unknown closure date is never auto-purged; campaign and contact legal hold | 0 = forever | Contact can no longer be called or matched by number; re-importing the same list creates fresh contacts. Suppression is separate and stays | needs legal input | `retention.py:205-235, 278`; `CHECKPOINT-15A-NOTES.md:94`; `campaign_service.py:142` |
| `RETENTION_AUDIT_LOG_DAYS` | Deletes `audit_log` rows by age | `created_at`; rows about a held contact/campaign stay | 0 = forever | Loses the audit trail (who changed what; kill-switch, suppression removals) | needs legal input | `retention.py:237-253, 279`; `DATA-INVENTORY.md:31`; `CHECKPOINT-15A-NOTES.md:98` |
| `RETENTION_PROCESSED_EVENT_DAYS` | Deletes `processed_event` rows (provider event ids) by age | `received_at` | Production: must be **0 or >= 7** (`_MIN_PROCESSED_EVENT_RETENTION_DAYS = 7`) | Webhook de-duplication: `claim_event` relies on the unique event id to ignore re-deliveries (`processed_event_service.py:1-8`; callers `webhook_service.py:73`, `dograh_webhook_service.py:49`). If an id is purged before the provider stops retrying, a re-delivery is processed again. The 7-day floor is an assumption; the real Dograh retry horizon is UNVERIFIED (`CHECKPOINT-15A-NOTES.md:93`) | technical minimum 7 (or 0); anything above is needs legal input | `config.py:41, 586-592`; `retention.py:255-261, 280` |
| `RETENTION_RUN_HISTORY_DAYS` | Deletes `retention_run` rows (counts only), never the current run | `started_at` | 0 = forever | Loses evidence that retention ran and how much it deleted | needs legal input (it is the proof of retention) | `retention.py:263-272, 281`; `DATA-INVENTORY.md:34` |

Notes:
1. Existing draft values in `.env.example:148-153` and `CHECKPOINT-15A-NOTES.md:92` (transcripts 180, analysis 365, contact 365, audit 730, processed events 90, run history 365) are the earlier author's proposals, marked "owner to set". They are quoted here as history, not adopted.
2. `erasure_log` has no retention setting: append-only, a trigger rejects UPDATE/DELETE, kept as evidence: `DATA-INVENTORY.md:33`.
3. The Dograh path also stores transcript text locally: `dograh_webhook_service.py:168-183` downloads the transcript lines and inserts one `ConversationMessage` row per line. So `RETENTION_TRANSCRIPT_DAYS` covers Dograh transcripts held in this database. Only the copy on Dograh's side is outside it (`DATA-INVENTORY.md:37`). `working_memory_snapshot` content is native-path only (see Decision A).
4. Redis working-memory cache (TTL 3600 s) is not deleted by erasure or retention; other Redis payloads were not exhaustively audited: `CHECKPOINT-15A-NOTES.md:103`.

## 3. ERASURE_FINGERPRINT_PEPPER [READ]
- Required in production; at least 32 characters (`_MIN_SECRET_LENGTH = 32`, `config.py:35`) and at least 8 distinct characters (`_MIN_DISTINCT_SECRET_CHARS = 8`, `config.py:36`); checked at `config.py:593-597`.
- Must differ from `JWT_SIGNING_KEY`, `TELEPHONY_WEBHOOK_SECRET`, `DOGRAH_WEBHOOK_SECRET`: `config.py:598-604`.
- Used as the HMAC-SHA256 key for the erasure_log fingerprint of a normalised number: `data_lifecycle/fingerprint.py:3, 18-20`.
- Losing or changing it means earlier erasures can no longer be recognised by `reapply_erasures`; back it up with the other secrets: `backend/.env.example:156-158`; `CHECKPOINT-15A-NOTES.md:104`.
- Generation hint in the repo: `openssl rand -base64 48` (`.env.example:157`). Never commit the value.

## 4. Ready-to-paste .env block (no guesses)
`backend/.env.example:143` says to leave a retention setting unset, not write an empty value. Therefore the blanks below are comments; remove the `#` only after the owner decides. Do not paste a secret into this file.
```
# --- Retention (days; 0 = keep forever; every line required in production) ---
# RETENTION_TRANSCRIPT_DAYS=        # needs legal input
# RETENTION_ANALYSIS_DAYS=          # needs legal input
# RETENTION_CONTACT_DAYS=           # needs legal input (counted from campaign close)
# RETENTION_AUDIT_LOG_DAYS=         # needs legal input
# RETENTION_PROCESSED_EVENT_DAYS=   # 0 or >= 7 (technical minimum); above that needs legal input
# RETENTION_RUN_HISTORY_DAYS=       # needs legal input
RETENTION_BATCH_SIZE=500
RETENTION_MAX_RUNTIME_SECONDS=300
# ERASURE_FINGERPRINT_PEPPER=       # set in the secret manager only: >=32 chars, >=8 distinct, differs from JWT/webhook secrets
```
(`RETENTION_BATCH_SIZE=500` and `RETENTION_MAX_RUNTIME_SECONDS=300` are the existing defaults: `config.py:556-557`.)

## 5. Questions the owner and lawyer must answer
See P0-7. This file sets nothing legal.

OWNER DECISION:
