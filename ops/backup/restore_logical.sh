#!/usr/bin/env bash
# restore_logical.sh -- restore a logical backup into a NEW database.
#
# Usage: restore_logical.sh --artifact FILE --target-db NAME --identity FILE
#                           [--overwrite-existing NAME] [--tolerance-pct N]
# Connection via PG* environment (PGHOST/PGPORT/PGUSER/PGPASSFILE), never arguments.
#   --target-db NAME          created fresh; the script refuses if it already exists
#   --overwrite-existing NAME DROPS database NAME first. Must equal --target-db. Prints a
#                             warning; use only on a deliberate full-restore of a lost database.
# Verifies the checksum BEFORE decrypting, restores with pg_restore --exit-on-error
# (--no-owner --no-acl: roles are provisioned by infrastructure, not by the dump), then checks
# the Alembic revision and row counts against the manifest (tolerance default 0: a restore
# must match exactly what the backup recorded, within the live-backup tolerance given).
# AFTER restoring you MUST follow the post-restore procedure in
# docs/runbooks/BACKUP-AND-RESTORE.md (kill switch first, erasures, DNC reconcile).

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"

artifact=""; target=""; overwrite=""; tol=0
set -- "${REST_ARGS[@]}"
while (( $# )); do
  case "$1" in
    --artifact) artifact="${2:?}"; shift 2 ;;
    --target-db) target="${2:?}"; shift 2 ;;
    --identity) BACKUP_AGE_IDENTITY_FILE="${2:?}"; export BACKUP_AGE_IDENTITY_FILE; shift 2 ;;
    --overwrite-existing) overwrite="${2:?}"; shift 2 ;;
    --tolerance-pct) tol="${2:?}"; shift 2 ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ -f $artifact && -n $target ]] || die "--artifact and --target-db are required"
[[ $target =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] || die "invalid database name"
manifest="$artifact.manifest.json"

"$HERE/verify_backup.sh" --quick "$artifact" >/dev/null

exists="$(psql_q -d postgres -c "select 1 from pg_database where datname = '$target'" || true)"
if [[ $exists == 1 ]]; then
  [[ $overwrite == "$target" ]] || die "database '$target' already exists; restore into a new name, or pass --overwrite-existing $target to DROP it"
  log "WARNING: dropping existing database '$target' as requested"
  dropdb -- "$target"
elif [[ -n $overwrite ]]; then
  die "--overwrite-existing given but '$target' does not exist"
fi
createdb -- "$target"

work=""; make_tmpdir work
decrypt_stream "$artifact" >"$work/dump"
started="$(date +%s)"
pg_restore --exit-on-error --no-owner --no-acl --dbname="$target" "$work/dump"
rm -f -- "$work/dump"

want_rev="$(json_get "$manifest" alembic_revision)"
if [[ $want_rev != None ]]; then
  got_rev="$(PGDATABASE="$target" psql_q -c "select version_num from alembic_version limit 1")"
  [[ $got_rev == "$want_rev" ]] || die "alembic revision after restore ($got_rev) differs from the manifest ($want_rev)"
fi
PGDATABASE="$target" python3 - "$manifest" "$tol" <<'PY'
import json, subprocess, sys
m = json.load(open(sys.argv[1])); tol = float(sys.argv[2]); bad = []
for table, want in m["table_row_counts"].items():
    got = int(subprocess.run(["psql", "-X", "-qAt", "-c", f'select count(*) from public."{table}"'],
                             capture_output=True, text=True, check=True).stdout)
    if abs(got - want) > want * tol / 100.0:
        bad.append(f"{table}: manifest={want} restored={got}")
if bad:
    print("row-count mismatch: " + "; ".join(bad), file=sys.stderr); sys.exit(1)
PY
summary "script=restore_logical status=ok target_db=$target alembic=$want_rev seconds=$(( $(date +%s) - started )) NEXT=post-restore-procedure"
