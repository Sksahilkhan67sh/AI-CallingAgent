#!/usr/bin/env bash
# verify_backup.sh -- verify a backup artifact.
#
# Usage: verify_backup.sh [--quick | --restore-test] [--identity FILE] [--tolerance-pct N] ARTIFACT
#   --quick (default)  checksum of the ENCRYPTED file against its manifest, manifest sanity.
#                      Needs no private key, so it can run on the backup host.
#   --identity FILE    also decrypt (needs the PRIVATE age key) and structurally verify:
#                      logical -> pg_restore --list ; base -> pg_verifybackup.
#   --restore-test     (implies --identity) restore into a scratch database (logical; uses the
#                      PG* environment, point it at a scratch server) or start the base backup
#                      on BACKUP_VERIFY_PORT (default 55433) and compare row counts with the
#                      manifest within --tolerance-pct (default 5; 0 = exact).
# Decrypted material lives only in a private 0700 temporary directory removed on exit.
# Exit 0 verified, non-zero otherwise (a single flipped byte fails).

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"

mode=quick; tol=5; artifact=""
set -- "${REST_ARGS[@]}"
while (( $# )); do
  case "$1" in
    --quick) mode=quick; shift ;;
    --restore-test) mode=restore; shift ;;
    --identity) [[ $# -ge 2 ]] || die "--identity needs a value"; BACKUP_AGE_IDENTITY_FILE="$2"; export BACKUP_AGE_IDENTITY_FILE; shift 2 ;;
    --tolerance-pct) [[ $# -ge 2 ]] || die "--tolerance-pct needs a value"; tol="$2"; shift 2 ;;
    -*) die "unknown option $1" ;;
    *) [[ -z $artifact ]] || die "one artifact only"; artifact="$1"; shift ;;
  esac
done
[[ -f $artifact ]] || die "artifact not found"
manifest="$artifact.manifest.json"
[[ -f $manifest ]] || die "manifest missing: $(basename -- "$manifest")"
json_valid "$manifest" || die "manifest is not valid JSON"

kind="$(json_get "$manifest" kind)"
want_sha="$(json_get "$manifest" sha256)"
want_size="$(json_get "$manifest" size_bytes)"
[[ "$(file_size "$artifact")" == "$want_size" ]] || die "size differs from the manifest"
[[ "$(sha256_of "$artifact")" == "$want_sha" ]] || die "CHECKSUM MISMATCH: the artifact is corrupt or was modified"
case "$kind" in logical|base) ;; *) die "unknown manifest kind" ;; esac
tool="$(json_get "$manifest" encryption.tool)"
if [[ $tool == age ]]; then
  head -c 21 -- "$artifact" | grep -q '^age-encryption.org/v1' || die "artifact is not an age file"
fi
amode="$(file_mode "$artifact")"
(( (8#$amode & 8#007) == 0 )) || log "WARNING: artifact is accessible by 'other' (mode $amode)"

if [[ $mode == quick && -z ${BACKUP_AGE_IDENTITY_FILE:-} ]]; then
  summary "script=verify_backup status=ok level=checksum kind=$kind artifact=$(basename -- "$artifact")"
  exit 0
fi

work=""; make_tmpdir work
level=structure
if [[ $kind == logical ]]; then
  decrypt_stream "$artifact" >"$work/dump"
  pg_restore --list "$work/dump" >/dev/null || die "pg_restore --list failed"
else
  mkdir "$work/pgdata"; chmod 700 "$work/pgdata"
  decrypt_stream "$artifact" | tar -xz -C "$work/pgdata"
  pg_verifybackup "$work/pgdata" >/dev/null || die "pg_verifybackup failed"
fi

counts_ok() { # counts_ok  (uses PG* env) compares live counts to manifest, honouring $tol
  python3 - "$manifest" "$tol" <<'PY'
import json, subprocess, sys
m = json.load(open(sys.argv[1])); tol = float(sys.argv[2])
bad = []
for table, want in m["table_row_counts"].items():
    got = int(subprocess.run(["psql", "-X", "-qAt", "-c", f'select count(*) from public."{table}"'],
                             capture_output=True, text=True, check=True).stdout)
    if abs(got - want) > want * tol / 100.0:
        bad.append(f"{table}: manifest={want} restored={got}")
if bad:
    print("row-count mismatch: " + "; ".join(bad), file=sys.stderr); sys.exit(1)
PY
}

if [[ $mode == restore ]]; then
  level=restore-test
  [[ -n ${BACKUP_AGE_IDENTITY_FILE:-} || $tool != age ]] || die "--restore-test needs --identity"
  if [[ $kind == logical ]]; then
    scratch="verify_scratch_$$"
    createdb -- "$scratch"
    # shellcheck disable=SC2064
    trap "dropdb --if-exists -- '$scratch' >/dev/null 2>&1 || true; _cleanup" EXIT
    pg_restore --exit-on-error --no-owner --no-acl --dbname="$scratch" "$work/dump"
    PGDATABASE="$scratch" counts_ok || die "restored row counts differ from the manifest"
    want_rev="$(json_get "$manifest" alembic_revision || true)"
    got_rev="$(PGDATABASE="$scratch" psql_q -c "select version_num from alembic_version limit 1" 2>/dev/null || true)"
    [[ $want_rev == None || $want_rev == "$got_rev" ]] || die "alembic revision differs from the manifest"
  else
    port="${BACKUP_VERIFY_PORT:-55433}"
    sock="$work/sock"; mkdir "$sock"
    pg_ctl -D "$work/pgdata" -w -t 120 -l "$work/pg.log" start \
      -o "-c port=$port -c listen_addresses='' -c unix_socket_directories='$sock' -c archive_mode=off -c max_wal_senders=0" >/dev/null
    # shellcheck disable=SC2064
    trap "pg_ctl -D '$work/pgdata' -m immediate stop >/dev/null 2>&1 || true; _cleanup" EXIT
    PGHOST="$sock" PGPORT="$port" PGDATABASE="$(json_get "$manifest" database)" counts_ok || die "restored row counts differ from the manifest"
  fi
fi
summary "script=verify_backup status=ok level=$level kind=$kind artifact=$(basename -- "$artifact")"
