#!/usr/bin/env bash
# restore_wal.sh -- PostgreSQL restore_command: restore_command = '/opt/ops/restore_wal.sh "%f" "%p"'
#
# Usage: restore_wal.sh FILENAME DESTPATH
# Environment: WAL_ARCHIVE_DIR (default $BACKUP_DEST/wal), BACKUP_AGE_IDENTITY_FILE (the
# PRIVATE key file; present only on a restore host).
#
# Exit 0: segment restored and checksum-verified. Exit 1: segment not in the archive (the
# normal "end of archive" answer). Exit 126: segment present but corrupt or undecryptable --
# PostgreSQL treats 126/127 as FATAL and aborts recovery instead of silently ending it.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"
[[ ${#REST_ARGS[@]} -eq 2 ]] || die "usage: restore_wal.sh FILENAME DESTPATH"
seg="${REST_ARGS[0]}"; out="${REST_ARGS[1]}"
[[ $seg =~ ^([0-9A-F]{24}(\.partial|\.[0-9A-F]{8}\.backup)?|[0-9A-F]{8}\.history)$ ]] || die "unexpected WAL file name"

load_env_file
dir="${WAL_ARCHIVE_DIR:-${BACKUP_DEST:+$BACKUP_DEST/wal}}"
[[ -n $dir ]] || die "WAL_ARCHIVE_DIR (or BACKUP_DEST) is required"

src=""
for cand in "$dir/$seg.age" "$dir/$seg.gpg" "$dir/$seg"; do
  [[ -f $cand ]] && { src="$cand"; break; }
done
if [[ -z $src ]]; then log "wal segment not in archive"; exit 1; fi
[[ -r $dir/$seg.sha256 ]] || { log "checksum sidecar missing for an archived segment"; exit 126; }

tmp="$out.restore.$$"
register_cleanup "$tmp"
if ! decrypt_stream "$src" | gunzip -c >"$tmp"; then
  log "archived segment could not be decrypted/decompressed"; exit 126
fi
[[ "$(sha256_of "$tmp")" == "$(cat -- "$dir/$seg.sha256")" ]] || { log "checksum mismatch on restored segment"; exit 126; }
mv -- "$tmp" "$out"
