# Checkpoint 15B -- Backup, PITR, restore and recovery verification

Labels: **VERIFIED** (executed here; command and result recorded), **UNVERIFIED**, **BLOCKED**,
**DOCUMENTED LIMITATION**, **DEFERRED**, **OWNER DECISION**. All timings and sizes are **sandbox
measurements**, never production figures. Nothing was run against production.

Branch `feature/checkpoint-15b-backup-restore`, from `develop` @ `f6233c0` (CP15A merged, PR #51).

## 1. Objective

Make a restore possible, safe and *proven*: encrypted backups, WAL archiving and point-in-time
recovery, restore procedures, a secondary copy of the do-not-call list and of erasures, a mandatory
post-restore procedure, and executed drills -- without new Python dependencies and without a schema
migration.

## 2. Preconditions and baseline (recorded BEFORE changing anything)

| Check | Result |
|---|---|
| CP15A merged into `develop` | **VERIFIED**: `git log` shows merge `f6233c0` (PR #51); `erasure_log`, `app.scripts.reapply_erasures`, the retention engine and `docs/CHECKPOINT-15A-NOTES.md` are present |
| `ruff check .` | clean |
| `mypy app` | clean (173 files) |
| `alembic heads` | `c15a1b2c3d4e` (single) |
| `pytest` | **1587 passed** |

Sandbox: Python 3.12 (production image is `python:3.11-slim`: **DOCUMENTED LIMITATION**, no 3.11 run),
PostgreSQL 16.15, age (Ubuntu package), shellcheck, bash 5.

## 3. Deliverables, each with its label

| Item | Label |
|---|---|
| `ops/backup/lib.sh` shared library: `set -Eeuo pipefail`, `umask 077`, EXIT-trap cleanup, production refusals (no recipient / unencrypted override / world-accessible destination / world-accessible env file), injectable time (`NOW_OVERRIDE`, `--now`), no `set -x`, no secret in any argument | **VERIFIED** (`tests/ops/test_ops_static.py`, `test_ops_safety.py`) |
| `pg_logical_backup.sh`: `pg_dump -Fc` piped into `age` (no plaintext file), SHA-256 + PII-free manifest, quick verify, optional upload hook | **VERIFIED** (drills A, C; tests) |
| `pg_base_backup.sh`: plain `pg_basebackup` -> `pg_verifybackup` **before** encryption -> tar+gzip -> encrypt; records the start WAL segment | **VERIFIED** (drills B, C, E) |
| `archive_wal.sh` / `restore_wal.sh`: atomic, idempotent, refuse different content, gzip+encrypt, name allow-list; exit 1 = missing, 126 = corrupt (FATAL to PostgreSQL) | **VERIFIED** (tests, drills B, E) |
| `postgresql.backup.conf`, `backup.env.example` (no secrets) | documented; archive settings **VERIFIED** inside the drills |
| `verify_backup.sh`: `--quick` (no private key), `--identity` (decrypt + structure), `--restore-test` (logical scratch DB and physical start-up) | **VERIFIED** (drills C, D) |
| `prune_backups.sh`: GFS (7/4/12, never newest 3), dry-run default, refuses unless fresh, WAL pruned only below the oldest retained base's start segment | **VERIFIED** (drill F, 28 freshness/prune tests incl. an independent reference implementation) |
| `check_backup_freshness.sh`: exit 0/1/2; logical, base, newest WAL age (RPO signal), manifest validity, **WAL gap detection**, `--gaps-only`; one-line JSON; no alert wiring (CP16) | **VERIFIED** (drills E, F) |
| `restore_logical.sh`: new database by default, checksum before decrypt, `--exit-on-error`, post-checks; overwrite needs an explicit matching flag | **VERIFIED** (drills A, G, H, I) |
| `restore_pitr.sh`: new data dir, `restore_command`, time / name / LSN / **latest** target, promote, non-production port, reports timeline and stop point; `--target-latest` refuses on a WAL gap | **VERIFIED** (drills B, E) |
| `app/scripts/post_restore_report.py`: read-only (**the transaction is `SET TRANSACTION READ ONLY`**, so writes are rejected by PostgreSQL), age-bucketed stranded contacts / attempts / analyses (ids + statuses only), newest timestamp per table, secret-check fingerprints | **VERIFIED** (drill J, `tests/ops/test_post_restore_report.py`) |
| `app/scripts/export_suppressions.py` + `reconcile_suppressions.py` + `app/services/dnc_copy.py`: encrypted, checksummed secondary DNC copy and an add-only reconcile (dry-run default, counts-only audit, canonical normalization, honours removals present in the restored data) | **VERIFIED** (drill G, 14 tests, 3 mutation checks) |
| **Erasure-fingerprint copy** (`--kind erasure`) and `reconcile_suppressions --kind erasure` (append-only into `erasure_log`) -- **added beyond the written scope**, see 4.1 | **VERIFIED** (drill H, tests) |
| `ops/backup/drill.sh` + `drill_seed.py`: throwaway cluster, throwaway keys, synthetic data, destroyed on exit | **VERIFIED** |
| `docs/runbooks/BACKUP-AND-RESTORE.md`, `DRILL-REPORT-TEMPLATE.md`, `docs/DATA-INVENTORY.md` Backups section | documented (the commands in the runbook are the ones the drills run) |
| No new Python dependency; **no migration** (`alembic heads` unchanged, `alembic check` clean) | **VERIFIED** |

## 4. Findings made while building (each fixed, each with a test)

1. **`erasure_log` lives in the database being restored.** Drill H: erase a person *after* the backup,
   restore the older backup -> the person is back **and** the log row is gone, so `reapply_erasures`
   had nothing to apply. The CP15A mechanism cannot protect an erasure newer than the restore point.
   Fix: a second encrypted copy of the erasure fingerprints and an append-only reconcile
   (`--kind erasure`); runbook step 3a. This is an addition to the specified scope, made because drill H is
   meaningless without it. **Owner: review.**
2. **`make_tmpdir` registered its cleanup inside a command substitution**, i.e. in a subshell, so a failed
   or killed base backup / verify / restore would have left the decrypted staging directory behind.
   Fixed (nameref, no subshell); `test_a_failed_base_backup_leaves_no_staging_data`.
3. **A kill between the artifact rename and the manifest left a finished-looking artifact without a
   verified manifest.** Fixed: the artifact and manifest are registered for cleanup until verification
   passes. `test_a_killed_run_leaves_no_partial_or_plaintext_file` (mutation-checked: removing the
   partial's cleanup fails it).
4. **`load_env_file` did not export** the variables it sourced, so a cron/`archive_command` run (bare
   environment, only `BACKUP_ENV_FILE`) silently dumped the **wrong database** (the default one).
   Failing-then-passing: `test_a_backup_runs_from_nothing_but_the_environment_file` first failed with
   `'postgres' == 'ops'`, passes after `set -a`. My drills had not caught it because they export
   everything themselves.
5. `prune_backups.sh` crashed (`KeyError`) on a base manifest without `start_wal_segment`; it now deletes
   no WAL in that case.
6. A lost mid-archive WAL segment is invisible to an age-only freshness check; added gap detection
   (per-timeline, crosses log-file boundaries, a new timeline is not a gap) and made `--target-latest`
   refuse on a gap, because with no recovery target PostgreSQL would otherwise end recovery silently early.

## 5. Executed drills (SANDBOX measurements -- not production figures)

Transcripts: `docs/runbooks/drill-results/2026-10-13-sandbox-drills.txt` (drills A-E, G-J, one run of
`ops/backup/drill.sh all`) and `2026-10-13-drill-F-freshness-prune.txt`. Synthetic data, throwaway cluster
(private socket) and throwaway age keys deleted on exit. The transcript contains no key material and no
phone number (scanned).

| Drill | What was proven | Result | Duration (sandbox) | Label |
|---|---|---|---|---|
| A | logical backup -> restore into a new database; per-table row counts **and row checksums** identical; `alembic current` = head; `alembic check` clean | PASS | 3 s | VERIFIED |
| B | base backup + WAL archive; restore points `t1`/`t2`; recovered to `t1`: t1 row present, **t2 row absent**, timeline 2, `pg_is_in_recovery() = false` | PASS | 9 s | VERIFIED |
| C | right key decrypts; wrong key fails; every artifact/WAL segment is age-encrypted and mode 0600; no plaintext dump/tar/partial in the destination; staging empty; `--restore-test` logical and physical both match the manifest and the scratch DB is dropped | PASS | 3 s | VERIFIED |
| D | one flipped byte fails (logical **and** base); with the checksum deliberately bypassed the encryption's own authentication still fails | PASS | 4 s | VERIFIED |
| E | `--target-latest` recovers everything; after deleting one archived segment `--target-latest` refuses (nothing extracted), PITR past it fails with PostgreSQL's `FATAL: recovery ended before configured recovery target was reached`, and `check_backup_freshness.sh` exits 2 reporting the gap | PASS | 12 s | VERIFIED |
| F | freshness exit codes at every threshold boundary with **injected time**; GFS selection equals an independent reference over 400 days x 2 backups/day; newest N never deleted; refuses when stale; WAL pruned only below the oldest retained start | PASS (28 tests) | 18 s | VERIFIED |
| G | DNC entries added after the backup are missing after the restore; reconcile dry-run changes nothing; apply adds exactly those; original rows untouched; one counts-only audit event | PASS | 6 s | VERIFIED |
| H | erased person returns from the older backup (number, transcript, analysis back) **and** `erasure_log` is empty; erasure copy + DNC copy reconcile; `reapply_erasures` re-scrubs number, transcript and analysis text, re-adds the DNC row, is idempotent | PASS | 7 s | VERIFIED |
| I | backup taken at the previous Alembic revision (`a83d13f0c415`) restored, `alembic upgrade head`, `alembic check` clean, old row survives | PASS | 3 s | VERIFIED |
| J | `post_restore_report` after the PITR restore lists the seeded stranded states (2 contacts, 1 attempt, 1 analysis), prints no phone number, and the database (counts + row checksums) is unchanged | PASS | 0 s | VERIFIED |

Sandbox sizes: logical dump about 44 KB (synthetic, near-empty schema), base backup about 5.4 MB. These say
nothing about production throughput (CP17).

## 6. Tests

`tests/ops/` -- **110 tests** (static/lint/--help, production refusals, credential canary, partial/plaintext
cleanup, manifests, WAL commands, freshness + prune, DNC/erasure copy, post-restore report, real drills).
Without the binaries they **skip with an explicit message** (**VERIFIED**: with `PG_BIN` pointing nowhere,
89 passed and 21 skipped). `age` and `shellcheck` are optional; `runuser` is needed when the suite runs as root
(the drills run PostgreSQL as the `postgres` OS user).

Final gates: `ruff check .` clean; `mypy app` clean (177 files); `alembic upgrade head` + `alembic check` clean,
single head `c15a1b2c3d4e` (no migration); `bash -n` and `shellcheck` clean on all 11 scripts; full `pytest`:
**1697 passed, 0 skipped** (1587 baseline + 110 new). No existing test was modified.

CI (`.github/workflows/ci.yml`, GitHub-hosted runner with `postgres:16` and `redis:7` service containers) is
**unchanged**. Whether that runner has `age`/`initdb` is **UNVERIFIED** (I cannot run CI from here); the ops tests
skip cleanly if not. Installing `age` in CI is an OWNER DECISION.

## 7. OWNER DECISIONS (values proposed here; please confirm)

| # | Decision | Proposed |
|---|---|---|
| O1 | Deployment target | self-hosted PostgreSQL 16 (reference scripts) **or** managed (mapping in the runbook, section 11) -- unknown |
| O2 | Backup destination and DNC/erasure-copy destination | two **different** failure domains, neither inside the application's bucket/account |
| O3 | RPO / RTO | <= 15 min / <= 4 h (RPO only holds while the DB writes; heartbeat in the runbook) |
| O4 | Retention | 7 daily / 4 weekly / 12 monthly logical + base; WAL as long as the oldest retained base needs it; DNC/erasure copies >= 7 days |
| O5 | Key custodians | two named people, independent `age` keys, offline, encrypt to both |
| O6 | Full verification host | monthly drill with a custodian key (default) **or** a third verification-only recipient on an isolated host |
| O7 | `age` in the application image | add the binary to the image that runs the 15-minute exports (the current Dockerfile has none) |
| O8 | Install `age` (and optionally shellcheck) in CI | not done |
| O9 | The erasure-fingerprint copy (finding 4.1) | accept as part of CP15B? |
| **L1** | **LEGAL: maximum age of a backup that may contain data the system promised to erase** | **not decided here.** Options and the tension are in the runbook, section 12; `reapply_erasures` after every restore is the only mitigation in place |

## 8. Known limitations (DOCUMENTED LIMITATION)

- Production deployment, a managed service, Kubernetes manifests, cron and `archive_command` under a real
  `postgres` service account were **not** exercised (the CronJob YAML and cron lines are examples). **UNVERIFIED**.
- The `gpg` encryption path is implemented in `lib.sh` but **not exercised** by any test or drill. **UNVERIFIED**; `age` is the supported path.
- `restore_pitr.sh` writes a minimal `postgresql.conf` when the backup has none (Debian-style config outside
  PGDATA); that fallback was not exercised. **UNVERIFIED**.
- An idle database archives no WAL, so the RPO signal needs the heartbeat from the runbook; the heartbeat's
  effect (segment volume with `archive_timeout = 300`) was not measured.
- WAL segment size is assumed to be 16 MB for gap detection (`WAL_SEGMENT_MB`).
- The backup host holds only the public key, so it can only do the `--quick` check; full verification needs a
  private key (O6).
- Manifest row counts come from a different snapshot than the dump: use `--tolerance-pct` for live-captured backups.
- A base backup exists **unencrypted** in a private 0700 staging directory until verified (PostgreSQL 16's
  `pg_verifybackup` cannot verify a tar). Use tmpfs / an encrypted volume (`BACKUP_STAGING_DIR`).
- Removals of DNC entries made **after** the restore point cannot be seen by reconcile; those numbers are re-added (safe direction).
- Backups are never edited for erasure/retention: a person stays in older backups until they expire (L1).
- The erasure copy and the removal-aware reconcile need the **same** `ERASURE_FINGERPRINT_PEPPER` and `JWT_SIGNING_KEY`.
- `verify_backup.sh`/`restore_*.sh` write decrypted data to a private temp directory: run them on a restore host with tmpfs / encrypted disk.
- Redis is not backed up (by design); the retry schedule and stranded contacts/attempts/analyses are not rebuilt by anything in the repo (CP12D).
- No Python 3.11 run (production image); `bash`/GNU coreutils (`date -d`, `stat -c`, `find -printf`) assumed.

## 9. Deferred / out of scope

CP15C recording backup (blocked on the provider-consent decision); alerting, dashboards and scheduling infrastructure (CP16:
only the freshness exit codes are delivered); Redis persistence flags and Docker hardening (CP16); the call-attempt
sweeper, DLQ and DB-driven retry schedule (CP12D); load/throughput testing (CP17); cloud provisioning/Terraform; Dograh-side
configuration backup (owner task); the legal decision L1.

## 10. Deploy steps

1. Decide O1-O9 and L1. Generate **two** age key pairs offline; give each custodian one; keep only the **public** keys on servers.
2. Provision the backup destination and the DNC/erasure-copy destination (different failure domains; write-only writer identity; immutability where offered).
3. Record the secrets (`ERASURE_FINGERPRINT_PEPPER`, `JWT_SIGNING_KEY`, ...) in the secret manager and run
   `python -m app.scripts.post_restore_report` once; **store the two `secret_checks` values**.
4. Install `age` (and `python3`) where the scripts run; copy `ops/backup/` to `/opt/ai-calling-agent/ops/backup/`;
   create `/etc/ai-calling-agent/backup.env` (0600, `postgres`) from `backup.env.example`.
5. **Enable WAL archiving -- this needs a PostgreSQL restart** (`wal_level`, `archive_mode`): include `postgresql.backup.conf`,
   restart in a window, check `pg_stat_archiver` (`last_archived_wal` advances, `failed_count = 0`).
6. Run `pg_base_backup.sh` immediately, then schedule the jobs in the runbook (logical daily, base weekly, heartbeat, DNC + erasure
   copies every 15 min, freshness check every 5 min, prune daily with the separate deleting identity).
7. **Run a restore drill** on a restore host with each custodian's key (procedure A, B, and the DNC/erasure reconcile) and record it with
   `DRILL-REPORT-TEMPLATE.md`. Do not rely on the backups until this has passed with production-sized data.

## 11. Rollback

Everything is additive: stop the cron jobs / CronJob and remove `archive_command` (`archive_mode = off` needs another restart;
until then a failing `archive_command` makes `pg_wal` grow, so disable it deliberately). The new Python modules/scripts are unused
unless invoked. There is no schema change to roll back. Backups already written stay readable with the private keys; deleting
them is a separate, deliberate step.

## 12. Acceptance checklist

- [x] CP15A merged (verified) and baseline recorded before changes
- [x] No new Python dependency; no migration; `alembic check` clean
- [x] ruff, mypy, bash -n, shellcheck clean; full pytest passes (1697)
- [x] Drills A-J executed with evidence in `docs/runbooks/drill-results/` (F via pytest)
- [x] Encrypted output only in production mode; partial/plaintext cleanup proven (incl. a kill)
- [x] DNC copy: encrypted, checksummed, add-only reconcile; erasure fingerprints survive a restore
- [x] Post-restore procedure documented in the mandatory order, each step with a pass/fail check
- [ ] Owner confirms O1-O9
- [ ] Legal decides L1
- [ ] Restore drill on production-sized data with both custodians' keys
- [ ] `age` available in the image / CI (O7, O8)
