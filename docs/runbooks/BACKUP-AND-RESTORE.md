# Backup, point-in-time recovery and restore runbook (CP15B)

Labels used below: **VERIFIED** (executed in the CP15B sandbox drills; evidence in
`docs/runbooks/drill-results/`), **UNVERIFIED**, **DOCUMENTED LIMITATION**, **DEFERRED**,
**OWNER DECISION**. Sandbox timings are sandbox measurements, never production figures.
Nothing here has been run against a production system.

Related: `docs/DATA-INVENTORY.md` (what personal data lives where), `docs/CHECKPOINT-15A-NOTES.md`
(retention / erasure / legal hold), `docs/CHECKPOINT-15B-NOTES.md` (what was proven and what was not),
`docs/runbooks/DRILL-REPORT-TEMPLATE.md`.

---

## 0. Read this first -- the five things that can ruin a restore

1. **Lose the age private key and every backup is unreadable.** Key custody is section 3.
2. **Restore without engaging the kill switch first and the system may call people who opted out
   or were erased.** Post-restore procedure, section 10, step 1.
3. **A restore to an older point loses newer do-not-call entries and newer erasures.** Both have a
   separate encrypted secondary copy (section 6). Reconcile them (section 10, steps 3-4).
4. **The application does not repair stranded work yet** (CP12D, the call-attempt sweeper, is
   open): contacts left `DIALING`/`RETRY_SCHEDULED`, attempts left `INITIATED`, analyses left
   `PROCESSING` stay that way. `post_restore_report` lists them; a human decides.
5. **`JWT_SIGNING_KEY` and `ERASURE_FINGERPRINT_PEPPER` are backup-critical** (section 4).

---

## 1. Scope and architecture

| Asset | Backed up? | How | Notes |
|---|---|---|---|
| PostgreSQL (system of record) | **Yes** | continuous WAL archiving + weekly base backup + daily logical dump | all encrypted with `age`; scripts in `ops/backup/` |
| `suppression` (global do-not-call) | **Yes, twice** | inside every Postgres backup **and** a separate 15-minute encrypted export | highest-impact loss scenario (section 6) |
| `erasure_log` fingerprints | **Yes, twice** | inside Postgres backups **and** a separate encrypted export | the log lives in the DB being restored, so a newer erasure disappears with it (section 6) |
| Redis | **No** (by design) | not durable, not a system of record | section 8: what is lost and how to rebuild |
| Secrets / configuration | In the **secret manager** | not in this repo, never in backups | section 4 lists the backup-critical ones |
| Alembic revision | In the DB (`alembic_version`) and in every backup manifest | | restore then `alembic upgrade head` |
| Infrastructure definitions | Outside this repo | **DEFERRED / owner task** | compose file here is a development reference |
| Dograh-side configuration (workflows, prompts, QA node, API keys, provider-side recordings/transcripts) | **No: outside this repository** | **DEFERRED / owner task: back up separately** | not restorable from anything here; the workflow UUID/trigger settings are in the secret manager |
| Recordings | Not stored by this system | CP15C is blocked on the provider-consent decision | n/a |

Failure domains: the database host, the **backup destination** and the **DNC/erasure copy
destination** must be three different failure domains (different account / region / provider
where possible). **OWNER DECISION:** the actual destinations.

Deployment target (self-hosted vs managed Postgres) is an **OWNER DECISION**; the scripts target
self-hosted PostgreSQL 16 with stock tools, section 11 maps them to managed services.

---

## 2. Targets, and how each is measured

| Target | Proposed (OWNER DECISION) | Measured by |
|---|---|---|
| RPO | <= 15 minutes | `check_backup_freshness.sh` -> `wal.age_seconds` (age of the newest archived WAL segment). Bounded by `archive_timeout` (300 s) plus copy delay, **only while the database writes**: schedule the heartbeat (section 5) or an idle database looks stale. Between segments the unarchived tail lives only on the database host. |
| RTO | <= 4 hours | restore drills (section 13): record the wall-clock of each procedure on production-sized data. The sandbox figures (a few seconds for tiny synthetic data) say nothing about production. |

The DNC and erasure copies have their own, tighter loss window: the export interval (15 min).

---

## 3. Encryption-key custody (top-level)

* Backups are encrypted with `age` to an **asymmetric recipient list**. The backup host and the
  database host hold **only public keys** (`BACKUP_AGE_RECIPIENT`). They can write backups; they
  cannot read them.
* The **private keys (identity files)** are held **offline by two named custodians** (OWNER
  DECISION: names), each with an **independent** key. Encrypt to **both** public keys
  (`BACKUP_AGE_RECIPIENT=age1custodianA...,age1custodianB...`): either custodian alone can
  decrypt, so one lost key is survivable. Store each private key in a different physical place
  (e.g. a password manager vault with two-person access, plus a sealed offline copy).
* Never place a private key in the repository, a ticket, chat, an environment variable on a
  backup host, or a CI secret. A restore host receives the identity file only for the duration of
  the restore (tmpfs, `chmod 600`, deleted afterwards).
* **Generating a pair** (on the custodian's machine, offline): `age-keygen -o custodianA.key`
  (prints the public key; the file is the private key).
* **Rotation:** generate a new pair, add the new public key to `BACKUP_AGE_RECIPIENT` (keep the
  old one until the oldest backup encrypted only to it has expired), confirm a new backup
  decrypts with the new key (`verify_backup.sh --identity NEWKEY ...`), then retire the old key
  after the last backup that needs it is gone. Old backups stay readable only with the old key:
  rotation does **not** re-encrypt them.
* **Loss procedure:** if one custodian's key is lost, the other still decrypts everything: issue
  a replacement pair immediately and rotate as above. If **both** are lost, **all existing
  backups are unrecoverable** (this is the design: encryption that cannot be bypassed). Start new
  backups with a new pair at once and treat it as a data-loss incident.
* **Verification that a key still works:** each drill (section 13) must decrypt a real backup with
  each custodian's key. A key never tested is assumed lost.

---

## 4. Secrets that are backup-critical

Store these in the secret manager with its own versioning and backup. They are **not** in any
database backup.

| Secret | Why it is backup-critical | What happens without the **same** value |
|---|---|---|
| `ERASURE_FINGERPRINT_PEPPER` | `reapply_erasures` recognises an erased person by an HMAC of their number | a new pepper matches **nothing**: erased people stay un-erased after a restore, **silently** |
| `JWT_SIGNING_KEY` | the CP14 `suppression.removed` audit events carry a fingerprint keyed with it; `reconcile_suppressions` uses it to honour deliberate removals | removals are no longer recognised: numbers a human removed are re-added (the safe direction) |
| `TELEPHONY_WEBHOOK_SECRET`, `DOGRAH_WEBHOOK_SECRET` | webhook authentication | webhooks rejected until both sides are re-keyed |
| `DOGRAH_API_KEY`, trigger UUID, base URL | dialing | no calls |
| Admin / operator credentials | admin API | locked out |
| Database / replication passwords | | no restore access |
| The age **private** keys | section 3 | no restore at all |

**Record the secret checks at deploy time**: `python -m app.scripts.post_restore_report` prints a
12-character fingerprint of the pepper and of the signing key (`secret_checks`). Store both values
with the secrets. After a restore, compare: a mismatch means the wrong secret is in place and
`reapply_erasures` would silently do nothing. **VERIFIED**: the report prints them and they change
when the secret changes (`tests/ops/test_post_restore_report.py`). In development the pepper is
derived from the JWT key; production requires an explicit pepper (CP15A validator).

---

## 5. Schedules, scripts and configuration

All scripts: `ops/backup/`. Copy `backup.env.example` to `/etc/ai-calling-agent/backup.env`
(owner `postgres`, mode 0600; the scripts refuse a world-accessible file). **No private key and no
password belongs in it**; passwords come from `PGPASSFILE`. `BACKUP_ENV` defaults to
`production`, which refuses unencrypted output, a missing recipient and a world-accessible
destination (**VERIFIED**, `tests/ops/test_ops_safety.py`).

| Job | Command | Suggested schedule | Identity |
|---|---|---|---|
| Logical backup | `ops/backup/pg_logical_backup.sh` | daily 02:15 | backup writer (write, no delete) |
| Base backup (PITR) | `ops/backup/pg_base_backup.sh` | weekly Sun 03:00 | backup writer + `REPLICATION` role |
| WAL archiving | `archive_command` (below) | continuous | `postgres` OS user |
| WAL heartbeat | `psql -qAt -c "SELECT pg_logical_emit_message(false,'backup-heartbeat','x')"` | every 5 min | any role that can connect |
| DNC copy | `python -m app.scripts.export_suppressions` | every 15 min | separate destination |
| Erasure copy | `python -m app.scripts.export_suppressions --kind erasure` | every 15 min | same destination as DNC |
| Freshness check | `ops/backup/check_backup_freshness.sh` | every 5 min -> monitoring (CP16 wires alerts) | read-only |
| Prune | `ops/backup/prune_backups.sh --apply` | daily after the backup | **a separate identity that may delete**; never the writer |
| Verify (structure) | `ops/backup/verify_backup.sh --identity KEY ARTIFACT` | monthly (section 13) | on a restore host |

cron (database host; adjust paths and the environment file):

```cron
# The backup scripts read /etc/ai-calling-agent/backup.env themselves (BACKUP_ENV_FILE) and
# export everything in it, so cron needs nothing else.
15 2 * * *   postgres  /opt/ai-calling-agent/ops/backup/pg_logical_backup.sh
0  3 * * 0   postgres  /opt/ai-calling-agent/ops/backup/pg_base_backup.sh
*/5 * * * *  postgres  psql -qAt -c "SELECT pg_logical_emit_message(false,'backup-heartbeat','x')" >/dev/null
# The Python exports read the application environment; `set -a` exports the file's variables.
*/15 * * * * app       bash -c 'set -a; . /etc/ai-calling-agent/app.env; set +a; cd /opt/ai-calling-agent/backend && python -m app.scripts.export_suppressions && python -m app.scripts.export_suppressions --kind erasure'
```

Kubernetes CronJob for the DNC + erasure copy (the application image; `DNC_DEST` is a volume on
**different** storage; `BACKUP_AGE_RECIPIENT` is a public key, a ConfigMap value, not a secret):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata: {name: dnc-secondary-copy}
spec:
  schedule: "*/15 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: export
              image: <application image with the `age` binary installed>
              command: ["sh", "-c", "python -m app.scripts.export_suppressions && python -m app.scripts.export_suppressions --kind erasure"]
              env:
                - {name: DNC_DEST, value: /dnc}
                - {name: BACKUP_ENV, value: production}
                - name: BACKUP_AGE_RECIPIENT
                  valueFrom: {configMapKeyRef: {name: backup-config, key: age_recipients}}
              envFrom: [{secretRef: {name: app-secrets}}]   # database URL etc.
              volumeMounts: [{name: dnc, mountPath: /dnc}]
          volumes: [{name: dnc, persistentVolumeClaim: {claimName: dnc-copy}}]
```

**The application image must contain the `age` binary** (it is a system tool, not a Python
dependency; the current Dockerfile does not install it: **OWNER DECISION / follow-up**).
Expire old DNC/erasure copies with the storage's lifecycle policy (the writer has no delete right);
keep at least 7 days of 15-minute copies.

### Enabling WAL archiving (needs a PostgreSQL RESTART)

1. Create the destination (a different failure domain) and `/etc/ai-calling-agent/backup.env`.
2. Include `ops/backup/postgresql.backup.conf` from `postgresql.conf`
   (`wal_level = replica`, `archive_mode = on`, `archive_command`, `archive_timeout = 300`,
   `max_wal_senders`). `wal_level` and `archive_mode` **require a restart**: schedule a window.
3. Restart, then `SELECT pg_switch_wal();` and confirm `pg_stat_archiver.last_archived_wal`
   advances and `failed_count` stays 0.
4. Run `pg_base_backup.sh` once **immediately**: archived WAL is useless without a base backup.
5. If the destination becomes unavailable `archive_command` fails and `pg_wal` grows until the disk
   fills: monitor free space on the database host (CP16) and treat a failing archiver as an incident.

`archive_wal.sh` is atomic (temp name -> fsync -> rename; checksum sidecar written first), idempotent
for identical content (PostgreSQL retries), refuses different content under an existing name,
compresses then encrypts, and rejects unexpected file names. `restore_wal.sh` exits **1** for a
segment that is not in the archive and **126** for a corrupt one, which PostgreSQL treats as FATAL
instead of silently ending recovery. **VERIFIED** (`tests/ops/test_ops_safety.py`, drills B and E).

---

## 6. The two copies that protect people

### 6.1 Do-not-call copy

`python -m app.scripts.export_suppressions` writes `DNC_DEST/dnc-<UTC>.csv.age` plus a manifest
(row count, newest `created_at`, SHA-256 of the encrypted file). Contents: normalized number,
source, reason, created_at, nothing else. The export streams into `age`; no plaintext file exists.
`python -m app.scripts.reconcile_suppressions --copy FILE --identity KEY [--apply]` re-adds
numbers missing from the restored database:

* **Only adds.** It never deletes or modifies an existing row. **VERIFIED** (drill G,
  `tests/ops/test_dnc_copy.py`).
* Dry-run by default, prints counts only (never numbers).
* Keeps the original `source`; the reason becomes `restore_reconcile`.
* The copy is verified (checksum, size, row count, columns) before anything is applied; a damaged
  copy is refused whole.
* **Deliberate removals:** CP14 admin removal writes a `suppression.removed` audit event with a keyed
  fingerprint. A number whose removal event is **in the restored database** and newer than the copy
  entry is **not** re-added. **DOCUMENTED LIMITATION:** a removal made *after* the restore point is
  not in the restored audit log and cannot be seen, so that number is re-added. Re-adding a number
  that was deliberately removed is the safe direction (an opt-out must never be missed); a human
  can remove it again. Requires the same `JWT_SIGNING_KEY` (section 4).

### 6.2 Erasure copy

`erasure_log` lives in the database that is being restored. An erasure made **after** the restore
point disappears from the restored database together with its log row, so `reapply_erasures` would
have nothing to apply and the person would come back un-erased. Drill H proved this exactly.
`export_suppressions --kind erasure` therefore copies the **fingerprints** (HMAC, never numbers) and
`reconcile_suppressions --kind erasure` appends the missing ones (append-only; `erasure_log` rejects
update/delete). Then `reapply_erasures` scrubs the people who returned. **VERIFIED** (drill H).

Limits: the erasure copy holds fingerprints, so the pepper must be the original (section 4); the
person's number returns to the DNC list through the DNC copy (erasure suppresses by default); data
that exists **only** in a backup older than the erasure is re-scrubbed on restore, but it stays in
the backup itself until that backup expires (legal question, section 12).

---

## 7. Backup artifacts and verification

| File | Content | Verified by |
|---|---|---|
| `logical/logical-<UTC>.dump.age` | `pg_dump -Fc`, encrypted | `verify_backup.sh` |
| `base/base-<UTC>.tar.gz.age` | `pg_basebackup` (plain, WAL streamed), tar+gzip, encrypted | `pg_verifybackup` before encryption; `verify_backup.sh` |
| `wal/<segment>.age` + `.sha256` | one gzip'd, encrypted WAL segment | `restore_wal.sh` re-checks the checksum |
| `*.manifest.json` beside each | UTC time, tool/server versions, DB name, **Alembic revision**, per-table row counts, size, **SHA-256 of the encrypted file**, encryption tool + recipient fingerprint, script version, (base) start WAL segment | no PII; **VERIFIED** (`tests/ops/test_ops_safety.py`) |

All files mode 0600. `verify_backup.sh` levels:

* `--quick` (default, **no private key needed**, runs on the backup host): manifest valid, size and
  SHA-256 of the encrypted file match, age header present.
* `--identity KEY`: also decrypts into a private 0700 temp directory and checks structure
  (`pg_restore --list` / `pg_verifybackup`).
* `--restore-test --identity KEY`: restores into a scratch database (logical; uses the `PG*`
  environment, point it at a **scratch** server) or starts the base backup on `BACKUP_VERIFY_PORT`
  and compares row counts to the manifest.

A single flipped byte fails the checksum, and even with the checksum bypassed the encryption's own
authentication fails (**VERIFIED**, drill D).

**DOCUMENTED LIMITATION:** the backup host holds only the public key, so it can only do the `--quick`
check; full verification needs a private key. Options (**OWNER DECISION**): (a) full verification
only in the monthly drill with a custodian's key (default); (b) add a third, verification-only
recipient whose key lives on an isolated verification host with read-only access to the backups
(more automation, one more key that can read everything).

Manifest row counts come from a separate snapshot than the dump, so on a **live** system counts can
differ slightly: `verify_backup.sh --tolerance-pct` (default 5) and `restore_logical.sh
--tolerance-pct` (default 0, exact: use it for drills/quiesced restores, raise it for live-captured
backups).

### Retention (OWNER DECISION: proposed 7 daily / 4 weekly / 12 monthly)

`prune_backups.sh` (dry-run by default; `--apply` required) keeps the newest of each of the last 7
days, 4 ISO weeks and 12 months per kind, **never** the newest 3 whatever the policy, refuses to run
unless `check_backup_freshness.sh` is healthy, and deletes WAL only below the start segment of the
oldest **retained** base backup (a manifest without a start segment deletes no WAL). **VERIFIED**
(`tests/ops/test_ops_freshness_prune.py`, drill F).

---

## 8. Redis: not backed up -- what is lost and how to rebuild

Redis is not durable in `docker-compose.yml` and is not a system of record. A Redis loss is **not** a
data-loss event for customers, but it strands in-flight work.

| Redis state | After a loss | Rebuild |
|---|---|---|
| Dial queue / dialer stream | queued jobs gone | contacts are still `PENDING` in Postgres: **re-run `POST /api/v1/campaigns/{id}/enqueue`** per active campaign (idempotent, rebuilds from Postgres) |
| `enqueued:<idempotency_key>` guards (1 h) | gone (fine) | if they survive and block a re-enqueue, delete the keys or wait <= 1 h |
| Retry schedule (ZSET) | **lost; contacts in `RETRY_SCHEDULED` are stranded** | **not rebuilt by anything in the repo** (CP12D, open). `post_restore_report` lists them; a human decides (re-enqueue as new, or close) |
| Contacts in `DIALING` / attempts in `INITIATED` | stranded | same: reported, never auto-repaired |
| Kill switch (Redis flag) | **cleared** | the env backstop `OUTBOUND_KILL_SWITCH=true` is read first and survives; re-enable the Redis flag through the admin API after the restore if you want it |
| Rate-limit counters, CPS slots, heartbeats | reset | harmless; rebuild on use |
| Working-memory cache (`ai:memory:*`, TTL 1 h) | gone | harmless (native-engine conversations only) |
| Pause state | not in Redis | Postgres `campaign.status` is the only pause state; a Redis restart cannot un-pause a campaign |

---

## 9. Procedures

Common preparation for every procedure: engage the kill switch first (section 10, step 1);
get a custodian to supply an identity file onto a tmpfs; work on a **restore host**, never the
production host; use a **new** database name / data directory so nothing is overwritten.

```bash
export BACKUP_DEST=/mnt/backups/ai-calling-agent
export WAL_ARCHIVE_DIR=$BACKUP_DEST/wal
export BACKUP_AGE_IDENTITY_FILE=/run/restore/custodianA.key      # tmpfs, mode 600
export PGHOST=... PGPORT=... PGUSER=... PGPASSFILE=/run/restore/pgpass   # never as arguments
```

### A. Full logical restore (data corruption confined to the database, newest daily dump is fine)

```bash
ART=$(ls -1 $BACKUP_DEST/logical/logical-*.dump.age | tail -n 1)
ops/backup/verify_backup.sh --identity "$BACKUP_AGE_IDENTITY_FILE" "$ART"      # structure check
ops/backup/restore_logical.sh --artifact "$ART" --target-db ai_calling_agent_restored \
    --identity "$BACKUP_AGE_IDENTITY_FILE" --tolerance-pct 5
# prints: SUMMARY ... status=ok target_db=... alembic=<revision> NEXT=post-restore-procedure
```

The script verifies the checksum before decrypting, restores with `pg_restore --exit-on-error
--no-owner --no-acl` (roles are provisioned by infrastructure), then checks the Alembic revision and
row counts against the manifest. Existing databases are never overwritten unless you pass
`--overwrite-existing NAME` **and** `NAME` equals `--target-db` (it prints a warning and drops it).
Loss window: up to the age of that dump (up to 24 h); use B to lose less. **VERIFIED** (drill A).
Then the post-restore procedure (section 10).

### B. Point-in-time recovery (PITR)

```bash
BASE=$(ls -1 $BACKUP_DEST/base/base-*.tar.gz.age | tail -n 1)       # newest base BEFORE the target
ops/backup/restore_pitr.sh --base "$BASE" --data-dir /var/lib/restore/pitr \
    --wal-dir "$WAL_ARCHIVE_DIR" --identity "$BACKUP_AGE_IDENTITY_FILE" --port 55434 \
    --target-time '2026-10-12 03:14:00+00'          # or --target-name NAME / --target-lsn LSN
```

It extracts to a **new** data directory (refuses a non-empty one), writes `restore_command`, the
target, `recovery_target_action = promote` and `recovery.signal`, starts the cluster on a **non-production
port** with archiving off and `listen_addresses = ''` (unix socket in `<data-dir>/.s`), waits until
recovery ends, and reports the new timeline and the recovery stop point. Choose the base backup that
**precedes** the target. If a WAL segment needed to reach the target is missing or corrupt, **recovery
fails loudly** (`FATAL: recovery ended before configured recovery target was reached`) and the script
exits non-zero. **VERIFIED** (drill B: row at restore point `t1` present, `t2` absent, timeline 2,
`pg_is_in_recovery() = false`; drill E: lost segment fails loudly).

Verify the result: `psql -h /var/lib/restore/pitr/.s -p 55434 -d <db>` and check the expected rows.
To put it into service, either dump the recovered data into a fresh production database (`pg_dump`
from the recovered cluster, `restore_logical.sh`-style restore), or stop the recovered cluster and
start it as the new primary with production settings (new `port`/`listen_addresses`, `archive_mode`
and `archive_command` pointing at the archive; the new timeline continues in the same archive).
Section 10 applies either way.

### C. Single table / row-level recovery (someone deleted a campaign, a bad import, ...)

Never restore over production. Restore to a scratch database and copy selectively:

```bash
ops/backup/restore_logical.sh --artifact "$ART" --target-db scratch_restore --identity "$BACKUP_AGE_IDENTITY_FILE"
# inspect, then copy only what is needed, e.g. one campaign's contacts:
psql -d scratch_restore -c "\copy (select * from contact where campaign_id = '<uuid>') to '/run/restore/contacts.csv' csv"
psql -d ai_calling_agent -c "\copy contact from '/run/restore/contacts.csv' csv"   # mind FK order and ON CONFLICT
```

Rules: **never copy `suppression` rows backwards in time** (use `reconcile_suppressions`, which only adds);
restoring `contact` / `conversation_*` / `call_analysis` rows can resurrect **erased people**, so run
`reapply_erasures` afterwards; deleting the scratch database ends the exposure of the plaintext copy
(`dropdb scratch_restore`; it is not encrypted at rest unless the server's storage is). Use the
read-only PITR restore (B) if the data you need is newer than the last dump.

### D. Total loss / new infrastructure

1. Provision PostgreSQL 16 and the application hosts from your infrastructure definitions (outside this repo).
2. Restore the secrets from the secret manager, **including** the pepper and signing key (section 4).
   Dograh-side configuration is restored separately (**owner task**).
3. Engage the kill switch in the new environment before anything starts (section 10, step 1).
4. Fetch the backup destination (a different failure domain) and a custodian's identity file.
5. Restore the newest base backup and **all** archived WAL:
   `ops/backup/restore_pitr.sh --base "$BASE" --data-dir ... --wal-dir "$WAL_ARCHIVE_DIR" --identity ... --target-latest`.
   `--target-latest` first runs `check_backup_freshness.sh --gaps-only` and **refuses if a WAL segment
   is missing** (without a target, a missing mid-archive segment would end recovery silently early);
   pass `--allow-gaps` only as a deliberate acceptance of the loss. **VERIFIED** (drill E).
   If the WAL archive is unusable, fall back to the newest logical dump (procedure A).
6. Continue with section 10. Expect to lose up to the RPO of data; the DNC/erasure copies (section 6)
   are what close the highest-risk part of that gap.

### E. Accidental destructive change (bad migration, bad retention run, bad bulk import)

1. **Stop the damage:** engage the kill switch; pause the retention schedule (`run_retention` is only
   run by your scheduler); stop the offending job.
2. **Find the moment.** Retention runs are recorded with exact times:
   ```sql
   SELECT id, mode, status, started_at, finished_at, counts
   FROM retention_run ORDER BY started_at DESC LIMIT 10;
   ```
   Each `--apply` run also has a counts-only `retention.run` audit event. For a migration use
   `alembic_version` history / deploy time. Pick `--target-time` a minute **before** `started_at`.
3. Run procedure B to that time on a restore host; **do not** promote it into production.
4. Compare and copy back only the damaged data (procedure C). Never copy `suppression` backwards.
5. Retention deletes are not reversible from the application: only a restore recovers them, which
   is why CP15A requires a dry run and legal holds before the first `--apply`.
6. Run `reapply_erasures` (section 10, step 3) after copying personal data back.

---

## 10. MANDATORY post-restore procedure (in this exact order)

Do not skip a step or reorder. Every step has a pass/fail check.

| # | Step | Command | PASS when |
|---|---|---|---|
| 1 | **Engage the kill switch BEFORE anything starts.** Redis is empty after a restore, so use the environment backstop. | set `OUTBOUND_KILL_SWITCH=true` in the environment of **every** process (API, dialer/queue workers, analysis worker), **then** start them. Optionally also `POST /api/v1/admin/kill-switch` once the API is up | after start: `GET /api/v1/admin/kill-switch` (admin) reports the switch active, and workers log `outbound_blocked_not_reading_queue` about once a minute. **If you cannot confirm it, do not continue.** |
| 2 | **Migrate forward and check.** | `cd backend && alembic upgrade head && alembic check` | `alembic heads` is one revision, `alembic check` prints `No new upgrade operations detected` (**VERIFIED** for a current backup, drill A, and for an OLDER backup restored then upgraded, drill I) |
| 3 | **Re-apply erasures.** 3a append the fingerprints that the restored `erasure_log` lacks; 3b scrub. | 3a `python -m app.scripts.reconcile_suppressions --kind erasure --copy <newest erasure-*.csv.age> --identity KEY` (dry run, review, then `--apply`). 3b `python -m app.scripts.reapply_erasures` (dry run), review, then `--apply` | 3a reports `would_add`/`added` counts; 3b dry run lists matched contacts, `--apply` scrubs them; re-running 3b matches 0. **VERIFIED** (drill H). Compare the `secret_checks` values with those recorded at deploy (section 4) **before** trusting a "0 matched" result |
| 4 | **Reconcile the DNC list** from the secondary copy. | `python -m app.scripts.reconcile_suppressions --copy <newest dnc-*.csv.age> --identity KEY` (**dry run**), review `would_add` and the two newest timestamps, then `--apply` | after apply, a second dry run prints `would_add: 0`; then `python -m app.scripts.reapply_erasures` shows `erased_missing_from_dnc: 0`. **VERIFIED** (drill G) |
| 5 | **Report stranded work; a human decides.** | `python -m app.scripts.post_restore_report` | read the newest-timestamp table (how much was lost) and the three stranded lists (contacts `DIALING`/`RETRY_SCHEDULED`, attempts `INITIATED`, analyses `PROCESSING`). **Nothing is repaired automatically** (CP12D open). **VERIFIED**: the report changes no data and runs in a read-only transaction (drill J) |
| 6 | **Rebuild Redis as far as the repo supports.** | per active campaign: `POST /api/v1/campaigns/{id}/enqueue` (idempotent; **only after** step 8 if you want it to dial: with the kill switch on, workers do not read the queue) | `PENDING` contacts are queued again. **Cannot be rebuilt:** the retry schedule and everything in section 8's "stranded" rows |
| 7 | **Smoke tests.** | `GET /health` and `/ready`; admin login; read-only list endpoints (campaigns, contacts, call attempts); open one call analysis; run one analysis claim with the analysis worker | all 2xx; the data you expect is there; the analysis worker claims and finishes (or skips) one job without error |
| 8 | **Release the kill switch only after written sign-off.** | remove `OUTBOUND_KILL_SWITCH`, restart workers; clear the Redis flag via the admin API if it was set | the sign-off (name, time, what was checked) is recorded **before** this step |
| 9 | **Record the incident and the evidence.** | fill `docs/runbooks/DRILL-REPORT-TEMPLATE.md` | the record exists |

---

## 11. Managed PostgreSQL (mapping)

If the deployment target is a managed service (OWNER DECISION), the stock-tool scripts are the
reference design and the provider's features replace parts of them:

| This runbook | Typical managed-service equivalent | Still the application's responsibility |
|---|---|---|
| WAL archiving + base backups + PITR (`archive_wal.sh`, `pg_base_backup.sh`, `restore_pitr.sh`) | native automated backups and **point-in-time restore** to a new instance | verify the retention window meets RPO, the restore **creates a new instance** (never in place), cross-region copy |
| Logical backups (`pg_logical_backup.sh`) | still useful: provider snapshots are same-account/same-provider; keep an **independent encrypted logical dump** to another failure domain | encryption with a key the provider does not hold; the `age` pipeline works unchanged against the managed endpoint over a network connection (no `REPLICATION` needed) |
| Encryption | provider encryption at rest | client-side `age` encryption of the independent dumps (section 3) |
| `check_backup_freshness.sh` | provider backup status API / metrics | map the provider's "latest restorable time" to the RPO check |
| DNC + erasure copies (section 6) | **unchanged**: provider PITR can lose entries newer than the restore point exactly like self-hosted | keep these copies on storage outside the provider account |
| Redis | managed Redis (decide persistence) | section 8 applies regardless |

Verify before relying on it: retention days, whether restores cross regions, restore **time** for your
data size (measure it), who can delete backups (use deletion protection / immutability), audit logging
of backup access. **UNVERIFIED** (no managed service was available).

---

## 12. Backup security, and the legal tension on backup age

* **Least privilege:** the backup writer can create files in the destination but cannot delete or
  overwrite them (object-store write-only role, or object lock / immutability / versioning with
  MFA-delete). Pruning uses a **separate** identity. The database role for backups has only what
  it needs (`pg_dump`: read; base backups: `REPLICATION`).
* **Different failure domain and account** from the database host; **never** inside the application's
  own bucket or credentials; access logging on; alert on any delete (CP16).
* The destination directory must not be accessible by "other" (the scripts enforce this in production mode).
* A restore host is temporary: encrypted disk or tmpfs, identity file removed afterwards, plaintext
  scratch databases dropped.
* `pg_basebackup` staging: PostgreSQL 16's `pg_verifybackup` needs a plain-format copy, so the base
  backup exists **unencrypted in a private 0700 directory** (`BACKUP_STAGING_DIR`; use tmpfs or an
  encrypted volume) until verified and encrypted; the EXIT trap removes it, including after a failure
  or a kill. **VERIFIED** (a failed base backup leaves no staging data;
  a killed logical backup leaves no partial or plaintext file: `tests/ops/test_ops_safety.py`).

**LEGAL DECISION (not made here): the maximum age of a backup that may contain data the system has
promised to erase.** A 12-month monthly backup keeps an erased person's data for up to a year after
the request. Options for the owner/legal:

1. **Shorter backup retention** (e.g. cap monthly backups) -- simple, loses long-term recovery.
2. **Crypto-shredding by key rotation:** encrypt each retention generation to its own recipient and
   destroy a generation's private key when it ages out; the backups become unreadable.
3. **Re-application on restore** (implemented): `reapply_erasures` (+ the erasure copy) scrubs erased
   people again whenever a backup is restored. This protects a **restored** system, not the backup
   *file*, which still contains the data until it expires.

Until decided, option 3 is the only technical mitigation in place: run section 10 step 3 after
**every** restore, including drills on real data.

---

## 13. Test cadence and drills

| Drill | When | What |
|---|---|---|
| Quick verification (`verify_backup.sh --quick`) | every backup (automatic) | checksum + manifest |
| Logical drill (procedure A + `--restore-test`) | **monthly** | full decrypt with **each** custodian's key, restore, row counts |
| Full PITR drill (procedure B incl. a lost-segment check) | **quarterly** | base + WAL, target time, timing for RTO |
| DNC / erasure reconcile drill (section 6) | quarterly | proves the copies are readable and reconcile works |
| After every **major migration** | each time | logical drill on a backup taken after the migration, and a restore of the one **before** it followed by `alembic upgrade head` |
| After any key / destination / `postgresql.conf` change | each time | the full set |

Record each drill with `docs/runbooks/DRILL-REPORT-TEMPLATE.md`. `ops/backup/drill.sh` runs the
sandbox drills (A-E, G-J) on a **throwaway** cluster and throwaway keys, with synthetic data:
`DRILL_PYTHON=$(which python) ops/backup/drill.sh all`. Its output for CP15B is in
`docs/runbooks/drill-results/` (sandbox measurements, **not** production figures).

### Verification checklist (after any restore)

- [ ] kill switch confirmed on before start (section 10 step 1)
- [ ] `alembic heads` one revision; `alembic check` clean
- [ ] erasure fingerprints reconciled; `reapply_erasures` re-run matches 0
- [ ] DNC reconciled; second dry run `would_add: 0`; `erased_missing_from_dnc: 0`
- [ ] `post_restore_report` reviewed; decision recorded for every stranded bucket
- [ ] `secret_checks` equal the values recorded at deploy
- [ ] redis state rebuilt as far as possible; the unrecoverable list acknowledged
- [ ] smoke tests pass; sign-off recorded **before** the kill switch is released
