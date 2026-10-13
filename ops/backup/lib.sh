#!/usr/bin/env bash
# Shared functions for the CP15B backup scripts. Source it; do not execute it.
#
# Rules every script relies on:
#   * secrets never appear on a command line or in a log line (use PG* env / PGPASSFILE and
#     identity FILES; this library never prints an environment value);
#   * output is encrypted unless BACKUP_ENV=development AND BACKUP_ALLOW_UNENCRYPTED=1;
#   * partial files and staging directories are removed by an EXIT trap, so a failed or
#     killed run leaves no plaintext behind;
#   * time is injectable (NOW_OVERRIDE) wherever age or freshness is decided.

BACKUP_SCRIPT_VERSION="15b.1"
umask 077

_CLEANUP_PATHS=()

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

_cleanup() {
  local p
  for p in "${_CLEANUP_PATHS[@]}"; do
    [[ -n $p ]] && rm -rf -- "$p"
  done
}
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

register_cleanup() { _CLEANUP_PATHS+=("$1"); }
# Once an artifact is complete AND verified, stop treating it as removable on failure.
unregister_cleanup() {
  local keep=() p
  for p in "${_CLEANUP_PATHS[@]}"; do [[ $p == "$1" ]] || keep+=("$p"); done
  _CLEANUP_PATHS=("${keep[@]}")
}

# A private (0700) scratch directory, removed on exit. Call as `make_tmpdir VARNAME` (NOT in
# a command substitution: the cleanup registration must happen in THIS shell, otherwise a
# failed or killed run would leave decrypted material behind). Prefer a tmpfs / encrypted
# volume via BACKUP_STAGING_DIR: this is where decrypted material briefly exists.
make_tmpdir() {
  local -n _out="$1"
  local base="${BACKUP_STAGING_DIR:-${TMPDIR:-/tmp}}" d
  mkdir -p -- "$base"
  d="$(mktemp -d -- "$base/cp15b.XXXXXX")"
  chmod 700 -- "$d"
  register_cleanup "$d"
  _out="$d"
}

now_epoch() {
  if [[ -n ${NOW_OVERRIDE:-} ]]; then date -u -d "$NOW_OVERRIDE" +%s; else date -u +%s; fi
}
fmt_ts() { date -u -d "@$1" +%Y%m%dT%H%M%SZ; }
fmt_iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
ts_now() { fmt_ts "$(now_epoch)"; }
iso_now() { fmt_iso "$(now_epoch)"; }

# 20261011T031500Z -> epoch seconds
ts_to_epoch() {
  local t="$1"
  date -u -d "${t:0:4}-${t:4:2}-${t:6:2} ${t:9:2}:${t:11:2}:${t:13:2} UTC" +%s
}

file_mode() { stat -c %a -- "$1"; }
file_size() { stat -c %s -- "$1"; }
file_mtime() { stat -c %Y -- "$1"; }
sha256_of() { sha256sum -- "$1" | cut -d' ' -f1; }

# JSON helpers use python3's standard library only (no packages).
json_get() { # json_get FILE dotted.key  -> prints the value, exits 1 when absent
  python3 - "$1" "$2" <<'PY'
import json, sys
node = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    node = node[part]
print(node if not isinstance(node, (dict, list)) else json.dumps(node))
PY
}
json_valid() { python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null; }

# ---- encryption ---------------------------------------------------------------------------

ENCRYPT_MODE=""   # age | gpg | none (set by require_safe_mode)

_recipient_list() { local IFS=,; read -ra _RECS <<<"${BACKUP_AGE_RECIPIENT:-}"; }

# Decides the encryption mode and enforces the production rules. $1 = destination directory
# (must already exist or be creatable).
require_safe_mode() {
  local dest="$1" env="${BACKUP_ENV:-production}" tool="${BACKUP_ENCRYPTION_TOOL:-}"
  if [[ -z $tool ]]; then
    if [[ -n ${BACKUP_AGE_RECIPIENT:-} ]]; then tool=age
    elif [[ -n ${BACKUP_GPG_RECIPIENT:-} ]]; then tool=gpg
    fi
  fi
  if [[ $env != development ]]; then
    [[ -z ${BACKUP_ALLOW_UNENCRYPTED:-} ]] || die "BACKUP_ALLOW_UNENCRYPTED is not permitted when BACKUP_ENV=$env"
    [[ -n $tool ]] || die "BACKUP_ENV=$env requires an encryption recipient (BACKUP_AGE_RECIPIENT or BACKUP_GPG_RECIPIENT)"
  fi
  case "$tool" in
    age)
      command -v age >/dev/null || die "age is not installed"
      [[ -n ${BACKUP_AGE_RECIPIENT:-} ]] || die "BACKUP_AGE_RECIPIENT is empty"
      ENCRYPT_MODE=age ;;
    gpg)
      command -v gpg >/dev/null || die "gpg is not installed"
      [[ -n ${BACKUP_GPG_RECIPIENT:-} ]] || die "BACKUP_GPG_RECIPIENT is empty"
      ENCRYPT_MODE=gpg ;;
    "")
      [[ ${BACKUP_ALLOW_UNENCRYPTED:-} == 1 ]] || die "no encryption recipient configured; for an UNENCRYPTED development run set BACKUP_ENV=development and BACKUP_ALLOW_UNENCRYPTED=1"
      log "WARNING: writing UNENCRYPTED output (development override). Never do this with real data."
      ENCRYPT_MODE=none ;;
    *) die "unknown BACKUP_ENCRYPTION_TOOL '$tool' (use age or gpg)" ;;
  esac
  if [[ -n $dest ]]; then
    mkdir -p -- "$dest"
    if [[ $env != development ]]; then
      local mode; mode="$(file_mode "$dest")"
      (( (8#$mode & 8#007) == 0 )) || die "destination $dest is accessible by 'other' (mode $mode); refusing in BACKUP_ENV=$env"
    fi
  fi
}

artifact_ext() { case "$ENCRYPT_MODE" in age) echo .age ;; gpg) echo .gpg ;; *) echo "" ;; esac; }

encrypt_stream() {
  case "$ENCRYPT_MODE" in
    age) _recipient_list
         local args=() r
         for r in "${_RECS[@]}"; do args+=(-r "$r"); done
         age "${args[@]}" ;;
    gpg) gpg --batch --yes --no-tty --trust-model always --encrypt --recipient "$BACKUP_GPG_RECIPIENT" ;;
    none) cat ;;
    *) die "encrypt_stream called before require_safe_mode" ;;
  esac
}

# decrypt_stream FILE : writes plaintext to stdout. Needs the PRIVATE key (identity file).
decrypt_stream() {
  case "$1" in
    *.age) [[ -n ${BACKUP_AGE_IDENTITY_FILE:-} && -r ${BACKUP_AGE_IDENTITY_FILE} ]] || die "BACKUP_AGE_IDENTITY_FILE is not set or not readable"
           age -d -i "$BACKUP_AGE_IDENTITY_FILE" "$1" ;;
    *.gpg) gpg --batch --yes --no-tty --decrypt "$1" ;;
    *) cat -- "$1" ;;
  esac
}

# Non-secret identifier of who can decrypt: sha256 of the recipient list, first 16 hex.
recipient_fingerprint() {
  case "$ENCRYPT_MODE" in
    age) printf '%s' "${BACKUP_AGE_RECIPIENT}" | sha256sum | cut -c1-16 ;;
    gpg) printf '%s' "${BACKUP_GPG_RECIPIENT}" | sha256sum | cut -c1-16 ;;
    *) echo none ;;
  esac
}

# ---- database helpers (credentials only via PG* env / PGPASSFILE) ----------------------------

psql_q() { psql -X -qAt -v ON_ERROR_STOP=1 "$@"; }

# write_manifest ARTIFACT KIND [EXTRA_JSON_OBJECT]
# Server-side JSON (so no JSON tooling is needed to write it). Contains NO PII: counts,
# versions, the Alembic revision, the checksum of the ENCRYPTED artifact.
write_manifest() {
  local artifact="$1" kind="$2" extra="${3:-{\}}" manifest="$1.manifest.json" tmp
  tmp="$(dirname -- "$artifact")/.$(basename -- "$artifact").manifest.partial.$$"
  register_cleanup "$tmp"
  psql_q -v kind="$kind" -v created="$(iso_now)" -v fname="$(basename -- "$artifact")" \
    -v sha="$(sha256_of "$artifact")" -v size="$(file_size "$artifact")" \
    -v tool="$ENCRYPT_MODE" -v rfp="$(recipient_fingerprint)" -v sver="$BACKUP_SCRIPT_VERSION" \
    -v dumpver="$(pg_dump --version | tr -d '\n')" -v extra="$extra" >"$tmp" <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT jsonb_pretty(
  jsonb_build_object(
    'format_version', 1,
    'kind', :'kind',
    'created_utc', :'created',
    'script_version', :'sver',
    'database', current_database(),
    'server_version', current_setting('server_version'),
    'tool_version', :'dumpver',
    'alembic_revision', (
      CASE WHEN to_regclass('public.alembic_version') IS NULL THEN NULL
           ELSE (xpath('/row/v/text()', query_to_xml('select version_num as v from public.alembic_version limit 1', false, true, '')))[1]::text END),
    'table_row_counts', (
      SELECT COALESCE(jsonb_object_agg(tablename,
        (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', schemaname, tablename), false, true, '')))[1]::text::bigint), '{}'::jsonb)
      FROM pg_tables WHERE schemaname = 'public'),
    'artifact', :'fname',
    'size_bytes', (:'size')::bigint,
    'sha256', :'sha',
    'encryption', jsonb_build_object('tool', :'tool', 'recipient_fingerprint', :'rfp')
  ) || (:'extra')::jsonb
);
COMMIT;
SQL
  chmod 600 -- "$tmp"
  mv -f -- "$tmp" "$manifest"
}

# Machine-readable one-line result (stdout). Keys/values must not contain secrets.
summary() { printf 'SUMMARY %s\n' "$*"; }

# Applies "--help" / "--now VALUE" handling common to every script. Prints usage from the
# script header comment block that starts at line 2 and runs to the first blank line.
common_args() {
  local self="$1"; shift
  REST_ARGS=()
  while (( $# )); do
    case "$1" in
      --help|-h) sed -n '2,/^$/p' -- "$self" | sed 's/^# \{0,1\}//'; exit 0 ;;
      --now) [[ $# -ge 2 ]] || die "--now needs a value"; NOW_OVERRIDE="$2"; export NOW_OVERRIDE; shift 2 ;;
      *) REST_ARGS+=("$1"); shift ;;
    esac
  done
}

# Loads a root-owned, postgres-readable environment file (archive_command runs with the
# postmaster's minimal environment). Never echoes its contents.
load_env_file() {
  local f="${BACKUP_ENV_FILE:-/etc/ai-calling-agent/backup.env}"
  if [[ -r $f ]]; then
    local mode; mode="$(file_mode "$f")"
    (( (8#$mode & 8#007) == 0 )) || die "$f is accessible by 'other' (mode $mode)"
    # `set -a` EXPORTS every variable in the file (PG*, BACKUP_*, WAL_ARCHIVE_DIR...) so that
    # pg_dump, psql and the encryptor see them: cron and archive_command start us with a bare
    # environment and nothing else.
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
  fi
}
