# Restore drill / incident report

Copy this file to `docs/runbooks/drill-results/<YYYY-MM-DD>-<scenario>.md` (or your incident
tracker) for every drill and every real restore. **Use synthetic data in drills. Never paste
phone numbers, keys, transcripts, connection strings or passwords into this report.** Durations
measured on anything other than production-sized data are *not* RTO evidence: say so.

| Field | Value |
|---|---|
| Date (UTC) | |
| Operator(s) | |
| Second person present (for any real restore) | |
| Type | drill / real incident |
| Scenario | A logical / B PITR / C single table / D total loss / E destructive change / other |
| Environment | sandbox / staging / production-sized rehearsal / production |
| Data | synthetic / production-sized copy / production (describe size, not content) |

## Backups used

| Item | Value |
|---|---|
| Logical backup artifact (name only) | |
| Base backup artifact (name only) | |
| WAL range archived (first -> last segment) | |
| Manifest SHA-256 matched? | yes / no |
| Alembic revision in the manifest | |
| Key used (custodian A / B), key tested this drill? | |
| DNC copy used (name only) / erasure copy used | |

## Steps and measured durations

| # | Step | Command (no secrets) | Started | Duration | Result |
|---|---|---|---|---|---|
| 1 | Kill switch engaged **before** anything started | | | | |
| 2 | Restore (A / B / C / D / E) | | | | |
| 3 | `alembic upgrade head`, `alembic check` | | | | |
| 4 | Erasure copy reconciled, `reapply_erasures` | | | | |
| 5 | DNC reconcile (dry run reviewed, then apply) | | | | |
| 6 | `post_restore_report` reviewed | | | | |
| 7 | Redis rebuild (`enqueue` per campaign) | | | | |
| 8 | Smoke tests | | | | |
| 9 | Kill switch released after sign-off | | | | |

**Measured RTO** (start of restore -> sign-off): `____` -- compared with the 4 h target: `____`
**Measured RPO** (newest data in the restored database vs the failure/target time): `____` -- compared with the 15 min target: `____`

## Verification

| Check | Result |
|---|---|
| Per-table row counts: restored vs manifest (list tables outside tolerance) | |
| Ordered-id / row checksum comparison (drills) | |
| `alembic current` / `alembic check` | |
| `secret_checks` equal the values recorded at deploy | yes / no |
| DNC: second reconcile dry run `would_add: 0`; `erased_missing_from_dnc: 0` | |
| `reapply_erasures`: second run matched 0 | |
| Stranded work (counts by bucket) and the decision taken for each | |
| Any plaintext left behind (restore host scratch DB / identity file removed)? | |

## Failures and surprises

| What happened | Impact | Fix / follow-up | Owner | Due |
|---|---|---|---|---|
| | | | | |

## Sign-off

| Role | Name | Decision (release the kill switch / not) | Time (UTC) |
|---|---|---|---|
| Engineering | | | |
| Owner / compliance (real restores) | | | |
