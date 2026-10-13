#!/usr/bin/env bash
# check_backup_freshness.sh -- is the newest backup recent enough? (alert wiring is CP16)
#
# Usage: check_backup_freshness.sh [--now ISO8601] [--gaps-only]
#   --gaps-only  only check the archived WAL sequence for missing segments (needs just
#                WAL_ARCHIVE_DIR); exit 2 when a segment is missing, else 0. Used by
#                restore_pitr.sh --target-latest so recovery never silently stops early.
# Environment: BACKUP_DEST, WAL_ARCHIVE_DIR (default $BACKUP_DEST/wal)
#   LOGICAL_WARN_HOURS=26  LOGICAL_CRIT_HOURS=50      newest logical backup age
#   BASE_WARN_HOURS=200    BASE_CRIT_HOURS=380        newest base backup age (weekly cadence)
#   WAL_WARN_MINUTES=15    WAL_CRIT_MINUTES=30        newest archived WAL segment age = RPO
# Exit 0 healthy, 1 warning, 2 critical. The last stdout line is a one-line JSON object.
# A missing or invalid manifest on the newest artifact is CRITICAL. A GAP in the archived WAL
# sequence (a lost segment: point-in-time recovery past it is impossible) is CRITICAL;
# WAL_SEGMENT_MB (default 16) must match the server's wal_segment_size. A quiet database
# archives no WAL: schedule a heartbeat (see the runbook) or the WAL check will report staleness.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"
gaps_only=0
for a in "${REST_ARGS[@]}"; do
  case "$a" in --gaps-only) gaps_only=1 ;; *) die "unexpected argument $a (see --help)" ;; esac
done
load_env_file
if (( gaps_only )); then
  walz="${WAL_ARCHIVE_DIR:?WAL_ARCHIVE_DIR is required with --gaps-only}"
else
  : "${BACKUP_DEST:?BACKUP_DEST is required}"
  walz="${WAL_ARCHIVE_DIR:-$BACKUP_DEST/wal}"
fi
# Missing segments between consecutive archived segments of the same timeline.
count_wal_gaps() {
  local per_log missing=0 prev_tl="" prev_n=0 seg tl n
  per_log=$(( 0x100000000 / (${WAL_SEGMENT_MB:-16} * 1024 * 1024) ))
  while IFS= read -r seg; do
    tl="${seg:0:8}"; n=$(( 16#${seg:8:8} * per_log + 16#${seg:16:8} ))
    if [[ $tl == "$prev_tl" ]] && (( n - prev_n > 1 )); then
      missing=$(( missing + n - prev_n - 1 ))
    fi
    prev_tl="$tl"; prev_n="$n"
  done < <(find "$walz" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sed -E 's/\.(age|gpg)$//' | grep -E '^[0-9A-F]{24}$' | sort -u)
  echo "$missing"
}
if (( gaps_only )); then
  missing="$(count_wal_gaps)"
  printf '{"status":"%s","wal_gaps":{"missing_segments":%d}}\n' "$([[ $missing == 0 ]] && echo ok || echo critical)" "$missing"
  (( missing == 0 )) && exit 0
  exit 2
fi
now="$(now_epoch)"
worst=0
parts=()

grade() { # grade AGE_SECONDS WARN_SECONDS CRIT_SECONDS -> prints 0/1/2
  if (( $1 >= $3 )); then echo 2; elif (( $1 >= $2 )); then echo 1; else echo 0; fi
}
bump() { (( $1 > worst )) && worst=$1; return 0; }

check_kind() { # check_kind KIND GLOB WARN_H CRIT_H
  local kind="$1" dir="$BACKUP_DEST/$1" newest age lvl created
  newest="$(find "$dir" -maxdepth 1 -type f -name "$kind-*" ! -name '*.manifest.json' ! -name '.*' 2>/dev/null | sort | tail -n 1 || true)"
  if [[ -z $newest ]]; then
    parts+=("\"$kind\":{\"status\":\"critical\",\"reason\":\"no backup found\"}"); bump 2; return
  fi
  if [[ ! -f $newest.manifest.json ]] || ! json_valid "$newest.manifest.json"; then
    parts+=("\"$kind\":{\"status\":\"critical\",\"reason\":\"manifest missing or invalid\",\"artifact\":\"$(basename -- "$newest")\"}"); bump 2; return
  fi
  created="$(json_get "$newest.manifest.json" created_utc)"
  age=$(( now - $(date -u -d "$created" +%s) ))
  (( age >= 0 )) || age=0
  lvl="$(grade "$age" $(( $3 * 3600 )) $(( $4 * 3600 )))"
  bump "$lvl"
  parts+=("\"$kind\":{\"status\":\"$(lvl_name "$lvl")\",\"age_seconds\":$age,\"artifact\":\"$(basename -- "$newest")\"}")
}
lvl_name() { case "$1" in 0) echo ok ;; 1) echo warning ;; *) echo critical ;; esac; }

check_kind logical "" "${LOGICAL_WARN_HOURS:-26}" "${LOGICAL_CRIT_HOURS:-50}"
check_kind base "" "${BASE_WARN_HOURS:-200}" "${BASE_CRIT_HOURS:-380}"

newest_wal="$(find "$walz" -maxdepth 1 -type f -name '[0-9A-F]*' ! -name '*.sha256' ! -name '*.history' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2- || true)"
if [[ -z $newest_wal ]]; then
  parts+=("\"wal\":{\"status\":\"critical\",\"reason\":\"no archived WAL segment\"}"); bump 2
else
  age=$(( now - $(file_mtime "$newest_wal") )); (( age >= 0 )) || age=0
  lvl="$(grade "$age" $(( ${WAL_WARN_MINUTES:-15} * 60 )) $(( ${WAL_CRIT_MINUTES:-30} * 60 )))"
  bump "$lvl"
  parts+=("\"wal\":{\"status\":\"$(lvl_name "$lvl")\",\"age_seconds\":$age}")
fi

missing="$(count_wal_gaps)"
if (( missing > 0 )); then
  parts+=("\"wal_gaps\":{\"status\":\"critical\",\"missing_segments\":$missing}"); bump 2
else
  parts+=("\"wal_gaps\":{\"status\":\"ok\",\"missing_segments\":0}")
fi

printf '{"status":"%s","exit_code":%d,"checked_utc":"%s",%s}\n' "$(lvl_name "$worst")" "$worst" "$(iso_now)" "$(IFS=,; echo "${parts[*]}")"
exit "$worst"
