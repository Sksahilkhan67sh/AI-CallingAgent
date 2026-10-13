#!/usr/bin/env bash
# pg_logical_backup.sh -- encrypted logical backup of the application database (pg_dump -Fc).
#
# Usage: pg_logical_backup.sh [--now ISO8601]
# Environment (see backup.env.example):
#   PGHOST/PGPORT/PGUSER/PGDATABASE and PGPASSFILE (or PGPASSWORD) -- never arguments
#   BACKUP_DEST            mounted destination; artifacts go to $BACKUP_DEST/logical/
#   BACKUP_AGE_RECIPIENT   age public key(s), comma separated   (or BACKUP_GPG_RECIPIENT)
#   BACKUP_ENV             production (default) | development
#   BACKUP_UPLOAD_CMD      optional executable called as: CMD ARTIFACT MANIFEST
#
# The dump is piped straight into the encryptor: no plaintext file is ever written.
# Writes logical-<UTC>.dump.age and its .manifest.json, then runs a quick verification
# (checksum + manifest). Full verification needs the PRIVATE key: see verify_backup.sh.
# Prints a final line: SUMMARY script=pg_logical_backup status=ok ...

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
dir="$BACKUP_DEST/logical"
mkdir -p -- "$dir"

name="logical-$(ts_now).dump$(artifact_ext)"
final="$dir/$name"
partial="$dir/.$name.partial.$$"
register_cleanup "$partial"
[[ ! -e $final ]] || die "$name already exists (refusing to overwrite)"

started="$(date +%s)"
pg_dump --format=custom --no-password | encrypt_stream >"$partial"
[[ -s $partial ]] || die "backup output is empty"
chmod 600 -- "$partial"
mv -- "$partial" "$final"
# From here until verification passes, a failure must not leave a finished-looking artifact.
register_cleanup "$final"; register_cleanup "$final.manifest.json"
write_manifest "$final" logical
manifest="$final.manifest.json"

"$HERE/verify_backup.sh" --quick "$final" >/dev/null
unregister_cleanup "$final"; unregister_cleanup "$final.manifest.json"
if [[ -n ${BACKUP_UPLOAD_CMD:-} ]]; then
  "$BACKUP_UPLOAD_CMD" "$final" "$manifest" >/dev/null || die "upload hook failed (exit $?)"
fi
summary "script=pg_logical_backup status=ok artifact=$name size=$(file_size "$final") sha256=$(sha256_of "$final") seconds=$(( $(date +%s) - started ))"
