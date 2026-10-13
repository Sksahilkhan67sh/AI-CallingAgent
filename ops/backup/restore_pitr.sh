#!/usr/bin/env bash
# restore_pitr.sh -- point-in-time recovery of a physical base backup into a NEW data directory.
#
# Usage: restore_pitr.sh --base FILE --data-dir DIR --wal-dir DIR --identity FILE [--port N]
#                        ( --target-time 'YYYY-MM-DD HH:MM:SS+00' | --target-name NAME | --target-lsn LSN
#                          | --target-latest [--allow-gaps] )
#                        [--timeout SECONDS]
# --data-dir must not exist or be empty. The new cluster starts on --port (default 55434, NOT
# the production port), listens on a unix socket inside --data-dir only, with archiving OFF,
# recovers to the target, promotes, and the script reports the new timeline and the stop point.
# --target-latest replays EVERYTHING archived (total-loss recovery). With no target a missing
# mid-archive segment would end recovery silently early, so it first runs
# check_backup_freshness.sh --gaps-only and refuses on any gap unless --allow-gaps.
# restore_command is restore_wal.sh (needs the PRIVATE key via --identity). If a WAL segment
# needed to reach the target is missing or corrupt, recovery FAILS (it never silently stops
# early) and this script exits non-zero.
# It does NOT touch any running cluster. Follow the post-restore procedure in the runbook
# before pointing the application at the result.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"

base=""; data=""; waldir=""; port=55434; timeout=600; tgt_kind=""; tgt_val=""; allow_gaps=0
set -- "${REST_ARGS[@]}"
while (( $# )); do
  case "$1" in
    --base) base="${2:?}"; shift 2 ;;
    --data-dir) data="${2:?}"; shift 2 ;;
    --wal-dir) waldir="${2:?}"; shift 2 ;;
    --identity) BACKUP_AGE_IDENTITY_FILE="${2:?}"; export BACKUP_AGE_IDENTITY_FILE; shift 2 ;;
    --port) port="${2:?}"; shift 2 ;;
    --timeout) timeout="${2:?}"; shift 2 ;;
    --target-time) tgt_kind="time"; tgt_val="${2:?}"; shift 2 ;;
    --target-name) tgt_kind=name; tgt_val="${2:?}"; shift 2 ;;
    --target-lsn) tgt_kind=lsn; tgt_val="${2:?}"; shift 2 ;;
    --target-latest) tgt_kind=latest; shift ;;
    --allow-gaps) allow_gaps=1; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ -f $base && -n $data && -d $waldir && -n $tgt_kind ]] || die "--base, --data-dir, --wal-dir and one recovery target are required"
[[ $tgt_kind == latest || $tgt_val =~ ^[A-Za-z0-9_:./+\ -]+$ ]] || die "recovery target contains unexpected characters"
if [[ -e $data ]] && [[ -n "$(ls -A -- "$data")" ]]; then die "data directory is not empty"; fi

"$HERE/verify_backup.sh" --quick "$base" >/dev/null
if [[ $tgt_kind == latest && $allow_gaps == 0 ]]; then
  WAL_ARCHIVE_DIR="$waldir" "$HERE/check_backup_freshness.sh" --gaps-only >/dev/null \
    || die "the WAL archive has a missing segment: recovery to the latest point would stop silently early. Fix the archive or pass --allow-gaps to accept the loss"
fi
mkdir -p -- "$data"; chmod 700 -- "$data"
decrypt_stream "$base" | tar -xz -C "$data"
rm -f -- "$data/postmaster.pid"
sock="$data/.s"; mkdir -p -- "$sock"; chmod 700 -- "$sock"
[[ -f $data/postgresql.conf ]] || : >"$data/postgresql.conf"

case "$tgt_kind" in
  time) tgt_line="recovery_target_time = '$tgt_val'" ;;
  name) tgt_line="recovery_target_name = '$tgt_val'" ;;
  lsn)  tgt_line="recovery_target_lsn = '$tgt_val'" ;;
  latest) tgt_line="# recover to the end of the archived WAL (no recovery target)" ;;
esac
{
  echo "# written by restore_pitr.sh"
  echo "restore_command = 'env WAL_ARCHIVE_DIR=\"$waldir\" BACKUP_AGE_IDENTITY_FILE=\"${BACKUP_AGE_IDENTITY_FILE:-}\" \"$HERE/restore_wal.sh\" \"%f\" \"%p\"'"
  echo "$tgt_line"
  echo "recovery_target_action = 'promote'"
  echo "recovery_target_inclusive = on"
  echo "archive_mode = off"
  echo "archive_command = ''"
  echo "port = $port"
  echo "listen_addresses = ''"
  echo "unix_socket_directories = '$sock'"
} >>"$data/postgresql.auto.conf"
touch "$data/recovery.signal"

started="$(date +%s)"
if ! pg_ctl -D "$data" -w -t "$timeout" -l "$data/restore.log" start >/dev/null; then
  tail -n 15 "$data/restore.log" >&2 || true
  die "cluster did not start / recovery failed (see $data/restore.log)"
fi
for _ in $(seq 1 "$timeout"); do
  inrec="$(PGHOST="$sock" PGPORT="$port" psql_q -d postgres -c 'select pg_is_in_recovery()' 2>/dev/null || echo t)"
  [[ $inrec == f ]] && break
  sleep 1
done
[[ $inrec == f ]] || die "recovery did not finish within ${timeout}s"
tl="$(PGHOST="$sock" PGPORT="$port" psql_q -d postgres -c 'select timeline_id from pg_control_checkpoint()')"
stop_line="$(grep -E 'recovery stopping' "$data/restore.log" | tail -n 1 | sed 's/^[^ ]* [^ ]* [^ ]* //' || true)"
summary "script=restore_pitr status=ok data_dir=$data socket_dir=$sock port=$port timeline=$tl in_recovery=false stop_point=\"${stop_line:-n/a}\" seconds=$(( $(date +%s) - started )) NEXT=post-restore-procedure"
