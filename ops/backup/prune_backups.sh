#!/usr/bin/env bash
# prune_backups.sh -- grandfather-father-son retention for logical and base backups.
#
# Usage: prune_backups.sh [--apply] [--now ISO8601]      (DRY RUN unless --apply)
# Environment: BACKUP_DEST, WAL_ARCHIVE_DIR (default $BACKUP_DEST/wal)
#   KEEP_NEWEST=3 (never deleted, whatever the policy)  KEEP_DAILY=7  KEEP_WEEKLY=4  KEEP_MONTHLY=12
# Policy per kind: keep the newest of each of the last KEEP_DAILY distinct days, of the last
# KEEP_WEEKLY distinct ISO weeks and of the last KEEP_MONTHLY distinct months, plus the
# newest KEEP_NEWEST. WAL segments are deleted only when older than the START WAL segment of
# the OLDEST retained base backup. Refuses to run unless check_backup_freshness.sh exits 0.
# Run it with an identity that may delete; the backup writer should not be able to.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"
apply=0
for a in "${REST_ARGS[@]}"; do
  case "$a" in --apply) apply=1 ;; *) die "unknown argument $a" ;; esac
done
load_env_file
: "${BACKUP_DEST:?BACKUP_DEST is required}"
walz="${WAL_ARCHIVE_DIR:-$BACKUP_DEST/wal}"

if ! "$HERE/check_backup_freshness.sh" >/dev/null 2>&1; then
  die "newest backups are not fresh (check_backup_freshness.sh is non-zero); refusing to prune"
fi

keep_newest="${KEEP_NEWEST:-3}"; kd="${KEEP_DAILY:-7}"; kw="${KEEP_WEEKLY:-4}"; km="${KEEP_MONTHLY:-12}"
deleted=0; kept=0
declare -A oldest_start=()

prune_kind() {
  local kind="$1" f base ts epoch day week month idx=0 reason
  declare -A seen_d=() seen_w=() seen_m=()
  local nd=0 nw=0 nm=0 oldest_kept=""
  mapfile -t files < <(find "$BACKUP_DEST/$kind" -maxdepth 1 -type f -name "$kind-*" ! -name '*.manifest.json' ! -name '.*' 2>/dev/null | sort -r)
  for f in "${files[@]}"; do
    base="$(basename -- "$f")"; ts="${base#"$kind"-}"; ts="${ts:0:16}"
    epoch="$(ts_to_epoch "$ts")"
    day="$(date -u -d "@$epoch" +%Y-%m-%d)"; week="$(date -u -d "@$epoch" +%G-W%V)"; month="$(date -u -d "@$epoch" +%Y-%m)"
    reason=""
    (( idx < keep_newest )) && reason="newest"
    if [[ -z ${seen_d[$day]:-} && $nd -lt $kd ]]; then seen_d[$day]=1; nd=$((nd+1)); reason="${reason:+$reason,}daily"; fi
    if [[ -z ${seen_w[$week]:-} && $nw -lt $kw ]]; then seen_w[$week]=1; nw=$((nw+1)); reason="${reason:+$reason,}weekly"; fi
    if [[ -z ${seen_m[$month]:-} && $nm -lt $km ]]; then seen_m[$month]=1; nm=$((nm+1)); reason="${reason:+$reason,}monthly"; fi
    idx=$((idx+1))
    if [[ -n $reason ]]; then
      echo "KEEP   $kind/$base ($reason)"; kept=$((kept+1)); oldest_kept="$f"
    else
      echo "DELETE $kind/$base"; deleted=$((deleted+1))
      if (( apply )); then rm -f -- "$f" "$f.manifest.json"; fi
    fi
  done
  if [[ $kind == base && -n $oldest_kept && -f $oldest_kept.manifest.json ]]; then
    # A manifest without a start segment (unknown bound) must delete NO wal, not crash.
    oldest_start[base]="$(json_get "$oldest_kept.manifest.json" start_wal_segment 2>/dev/null || true)"
  fi
}

prune_kind logical
prune_kind base

wal_deleted=0
if [[ -n ${oldest_start[base]:-} && -d $walz ]]; then
  [[ ${oldest_start[base]} =~ ^[0-9A-F]{24}$ ]] || die "base manifest has an invalid start_wal_segment; refusing to prune WAL"
  limit="${oldest_start[base]:8:16}"
  while IFS= read -r w; do
    name="$(basename -- "$w")"; name="${name%.sha256}"; name="${name%.age}"; name="${name%.gpg}"
    [[ $name =~ ^[0-9A-F]{24}$ ]] || continue
    if [[ ${name:8:16} < $limit ]]; then
      echo "DELETE wal/$(basename -- "$w")"; wal_deleted=$((wal_deleted+1))
      if (( apply )); then rm -f -- "$w"; fi
    fi
  done < <(find "$walz" -maxdepth 1 -type f ! -name '.*' | sort)
else
  echo "NOTE   no retained base backup with a start WAL segment: no WAL is deleted"
fi
summary "script=prune_backups status=ok mode=$([[ $apply == 1 ]] && echo apply || echo dry-run) kept=$kept deleted=$deleted wal_deleted=$wal_deleted"
