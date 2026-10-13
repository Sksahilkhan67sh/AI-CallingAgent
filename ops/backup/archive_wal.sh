#!/usr/bin/env bash
# archive_wal.sh -- PostgreSQL archive_command: archive_command = '/opt/ops/archive_wal.sh "%p" "%f"'
#
# Usage: archive_wal.sh PATH FILENAME
# Environment (usually from $BACKUP_ENV_FILE, readable by the postgres OS user only):
#   WAL_ARCHIVE_DIR (default $BACKUP_DEST/wal), BACKUP_AGE_RECIPIENT, BACKUP_ENV
#
# Atomic: the compressed+encrypted segment is written under a temporary name, fsync'd and
# renamed; the checksum sidecar is written first so the rename is the commit point.
# Re-archiving identical content succeeds (PostgreSQL retries); DIFFERENT content for an
# existing name is an error. Any failure exits non-zero so PostgreSQL keeps the segment and
# retries (a silently lost segment would break point-in-time recovery).

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"
[[ ${#REST_ARGS[@]} -eq 2 ]] || die "usage: archive_wal.sh PATH FILENAME"
src="${REST_ARGS[0]}"; seg="${REST_ARGS[1]}"

[[ $seg =~ ^([0-9A-F]{24}(\.partial|\.[0-9A-F]{8}\.backup)?|[0-9A-F]{8}\.history)$ ]] || die "unexpected WAL file name"
[[ -f $src ]] || die "source file does not exist"

load_env_file
dir="${WAL_ARCHIVE_DIR:-${BACKUP_DEST:+$BACKUP_DEST/wal}}"
[[ -n $dir ]] || die "WAL_ARCHIVE_DIR (or BACKUP_DEST) is required"
require_safe_mode "$dir"

dest="$dir/$seg$(artifact_ext)"
side="$dir/$seg.sha256"
sha="$(sha256_of "$src")"

if [[ -e $dest ]]; then
  [[ -r $side && "$(cat -- "$side")" == "$sha" ]] || die "an archived segment with this name has DIFFERENT content; refusing to overwrite"
  exit 0
fi

tmp="$dir/.$seg.tmp.$$"; tmps="$dir/.$seg.sha256.tmp.$$"
register_cleanup "$tmp"; register_cleanup "$tmps"
gzip -c -- "$src" | encrypt_stream >"$tmp"
[[ -s $tmp ]] || die "encrypted output is empty"
printf '%s' "$sha" >"$tmps"
sync -- "$tmp" "$tmps"
mv -f -- "$tmps" "$side"
mv -- "$tmp" "$dest"
sync -- "$dir"
