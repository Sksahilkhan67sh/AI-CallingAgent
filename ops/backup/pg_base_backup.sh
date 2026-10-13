#!/usr/bin/env bash
# pg_base_backup.sh -- encrypted physical base backup for point-in-time recovery.
#
# Usage: pg_base_backup.sh [--now ISO8601]
# Environment: as pg_logical_backup.sh, plus
#   BACKUP_STAGING_DIR   private scratch space (tmpfs / encrypted volume recommended).
#                        PostgreSQL 16's pg_verifybackup needs a PLAIN-format copy, so the
#                        base backup exists unencrypted in a 0700 directory until it has been
#                        verified and encrypted; the EXIT trap removes it even on failure.
# The connecting role needs the REPLICATION attribute.
#
# Steps: pg_basebackup (plain, WAL streamed) -> pg_verifybackup -> tar+gzip -> encrypt ->
# checksum + manifest (records the start WAL segment) -> copy to $BACKUP_DEST/base/.

set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"
[[ ${#REST_ARGS[@]} -eq 0 ]] || die "unexpected arguments (see --help)"

load_env_file
: "${BACKUP_DEST:?BACKUP_DEST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
require_safe_mode "$BACKUP_DEST"
dir="$BACKUP_DEST/base"
mkdir -p -- "$dir"

stage=""; make_tmpdir stage
name="base-$(ts_now).tar.gz$(artifact_ext)"
final="$dir/$name"
partial="$dir/.$name.partial.$$"
register_cleanup "$partial"
[[ ! -e $final ]] || die "$name already exists (refusing to overwrite)"

started="$(date +%s)"
pg_basebackup --pgdata="$stage/pgdata" --format=plain --wal-method=stream \
  --checkpoint=fast --label="cp15b-$(ts_now)" --no-password
pg_verifybackup "$stage/pgdata" >/dev/null || die "pg_verifybackup failed on the staging copy"

start_seg="$(sed -n 's/^START WAL LOCATION: .*(file \([0-9A-F]\{24\}\)).*/\1/p' "$stage/pgdata/backup_label")"
[[ -n $start_seg ]] || die "could not read the start WAL segment from backup_label"

tar -C "$stage/pgdata" -cz . | encrypt_stream >"$partial"
[[ -s $partial ]] || die "backup output is empty"
chmod 600 -- "$partial"
rm -rf -- "$stage/pgdata"
mv -- "$partial" "$final"
# From here until verification passes, a failure must not leave a finished-looking artifact.
register_cleanup "$final"; register_cleanup "$final.manifest.json"
write_manifest "$final" base "{\"start_wal_segment\": \"$start_seg\"}"

"$HERE/verify_backup.sh" --quick "$final" >/dev/null
unregister_cleanup "$final"; unregister_cleanup "$final.manifest.json"
if [[ -n ${BACKUP_UPLOAD_CMD:-} ]]; then
  "$BACKUP_UPLOAD_CMD" "$final" "$final.manifest.json" >/dev/null || die "upload hook failed (exit $?)"
fi
summary "script=pg_base_backup status=ok artifact=$name size=$(file_size "$final") start_wal=$start_seg sha256=$(sha256_of "$final") seconds=$(( $(date +%s) - started ))"
