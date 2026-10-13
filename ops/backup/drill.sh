#!/usr/bin/env bash
# drill.sh -- executed backup / restore drills on a THROWAWAY PostgreSQL cluster.
#
# Usage: drill.sh [A|B|C|D|E|G|H|I|J|all]
#   A logical round trip        B physical PITR to a named restore point
#   C encryption                D corruption detection
#   E lost WAL segment          G DNC loss + reconcile
#   H erasure re-application    I old backup, newer code (alembic)
#   J post-restore report is read-only        (F, freshness/prune, runs in pytest)
# Environment: DRILL_PYTHON (python with the app installed; default python3),
#   PG_BIN (default /usr/lib/postgresql/16/bin), DRILL_PORT (55432), DRILL_BASE (/tmp).
# The cluster, throwaway age keys and every artifact live in one temporary directory that is
# deleted on exit; the normal test database/Redis are never touched. Synthetic data only.
# All durations are SANDBOX measurements, not production figures.

# shellcheck disable=SC2015,SC2001  # `A && ok || { FAIL; }` is the intended assertion idiom here
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -eq 0 ]]; then exec runuser -m -u postgres -- "$0" "$@"; fi
# shellcheck source=lib.sh
source "$HERE/lib.sh"
common_args "${BASH_SOURCE[0]}" "$@"

PG_BIN="${PG_BIN:-/usr/lib/postgresql/16/bin}"; export PATH="$PG_BIN:$PATH"
PY="${DRILL_PYTHON:-python3}"
BACKEND="$(cd -- "$HERE/../../backend" && pwd)"
PORT="${DRILL_PORT:-55432}"; PITR_PORT=$((PORT + 2))
D="$(mktemp -d "${DRILL_BASE:-/tmp}/cp15b-drill.XXXXXX")"; export HOME="$D"
SOCK="$D/sock"; IDENT="$D/keys/a.key"; WRONG="$D/keys/wrong.key"
mkdir -p "$SOCK" "$D/keys" "$D/dest" "$D/stage" "$D/dnc"

drill_cleanup() {
  for dir in "$D/pitr" "$D/pitr-latest" "$D/pgdata"; do
    [[ -f $dir/postmaster.pid ]] && pg_ctl -D "$dir" -m immediate stop >/dev/null 2>&1 || true
  done
  _cleanup
  [[ ${DRILL_KEEP:-} == 1 ]] || rm -rf -- "$D"
}
trap drill_cleanup EXIT

age-keygen -o "$IDENT" >/dev/null 2>&1; age-keygen -o "$WRONG" >/dev/null 2>&1
export BACKUP_ENV=production BACKUP_AGE_RECIPIENT; BACKUP_AGE_RECIPIENT="$(age-keygen -y "$IDENT")"
export BACKUP_DEST="$D/dest" WAL_ARCHIVE_DIR="$D/dest/wal" BACKUP_STAGING_DIR="$D/stage"
export PGHOST="$SOCK" PGPORT="$PORT" PGUSER=postgres PGDATABASE=drill
export BACKUP_ENV_FILE="$D/backup.env"
printf 'BACKUP_ENV=production\nBACKUP_AGE_RECIPIENT=%s\nWAL_ARCHIVE_DIR=%s\n' "$BACKUP_AGE_RECIPIENT" "$WAL_ARCHIVE_DIR" >"$BACKUP_ENV_FILE"
mkdir -p "$WAL_ARCHIVE_DIR"

app() { # app DB CMD... : run with the application pointed at DB on the drill cluster
  local db="$1" host="$SOCK" port="$PORT"; shift
  if [[ $db == pitr ]]; then db=drill; host="$D/pitr/.s"; port="$PITR_PORT"; fi
  env "PRIMARY_DB_URL=postgresql+psycopg://postgres@/$db?host=$host&port=$port" \
      REDIS_URL=redis://localhost:6379/9 "PYTHONPATH=$BACKEND" "$@"
}
alembic_in() { local db="$1"; shift; ( cd "$BACKEND" && app "$db" "$PY" -m alembic "$@" ); }
seed() { app "$1" "$PY" "$HERE/drill_seed.py" "${@:2}"; }
q() { psql -X -qAt -v ON_ERROR_STOP=1 "$@"; }
check() { local msg="$1"; shift; if "$@" >/dev/null 2>&1; then echo "  ok:   $msg"; else echo "  FAIL: $msg"; return 1; fi; }
must_fail() { local msg="$1"; shift; if "$@" >/dev/null 2>&1; then echo "  FAIL: $msg (it succeeded)"; return 1; else echo "  ok:   $msg (failed as required)"; fi; }
run() { echo "  \$ $*"; "$@"; }

db_fingerprint() { # count + md5 of every row of every public table
  q -d "$1" <<'SQL'
select string_agg(format('%s:%s:%s', tablename, c, h), E'\n' order by tablename) from (
  select tablename,
    (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I', tablename), false, true, '')))[1]::text as c,
    (xpath('/row/h/text()', query_to_xml(format('select md5(coalesce(string_agg(t::text, ''|'' order by t::text), '''')) as h from %I t', tablename), false, true, '')))[1]::text as h
  from pg_tables where schemaname = 'public') s;
SQL
}
wait_archived() { # force a segment switch and wait until it is in the archive
  local seg
  seg="$(q -d postgres -c "select pg_walfile_name(pg_switch_wal() - 1)")"
  for _ in $(seq 1 60); do
    [[ "$(q -d postgres -c "select coalesce(last_archived_wal >= '$seg', false) from pg_stat_archiver")" == t ]] && return 0
    sleep 1
  done
  echo "segment $seg was not archived in 60 s"; return 1
}
marker() { q -d "${2:-drill}" -c "insert into campaign (id, name, status) values (gen_random_uuid(), '$1', 'active')"; }
latest() { find "$D/dest/$1" -maxdepth 1 -type f -name "$1-*" ! -name '*.manifest.json' ! -name '.*' | sort | tail -n 1; }

start_cluster() {
  initdb -D "$D/pgdata" -U postgres --auth=trust -E UTF8 >/dev/null
  cat >>"$D/pgdata/postgresql.conf" <<CONF
port = $PORT
listen_addresses = ''
unix_socket_directories = '$SOCK'
wal_level = replica
archive_mode = on
archive_command = '$HERE/archive_wal.sh "%p" "%f"'
max_wal_senders = 4
CONF
  pg_ctl -D "$D/pgdata" -w -l "$D/pg.log" start >/dev/null
  createdb drill
  alembic_in drill upgrade head >/dev/null 2>&1
}

drillA() {
  seed drill base; seed drill person +919800000301
  run "$HERE/pg_logical_backup.sh"
  local art; art="$(latest logical)"
  run "$HERE/restore_logical.sh" --artifact "$art" --target-db drill_restored --identity "$IDENT" --tolerance-pct 0
  [[ "$(db_fingerprint drill)" == "$(db_fingerprint drill_restored)" ]] && echo "  ok:   per-table row counts and row checksums identical" || { echo "  FAIL: fingerprint differs"; return 1; }
  db_fingerprint drill_restored | sed 's/^/        /'
  echo "  \$ alembic current / check (restored database)"
  alembic_in drill_restored current 2>/dev/null | tail -n 1
  alembic_in drill_restored check 2>&1 | tail -n 1 | grep -q "No new upgrade operations" && echo "  ok:   alembic check clean"
}

drillB() {
  [[ -f $D/pitr/PG_VERSION ]] && return 0   # drill B already produced the recovered cluster
  [[ -n $(find "$D/dest/logical" -name 'logical-*' 2>/dev/null) ]] || { seed drill base; }
  run "$HERE/pg_base_backup.sh"
  marker t1-marker; q -d drill -c "select pg_create_restore_point('t1')" >/dev/null
  sleep 1
  marker t2-marker; q -d drill -c "select pg_create_restore_point('t2')" >/dev/null
  wait_archived
  run "$HERE/restore_pitr.sh" --base "$(latest base)" --data-dir "$D/pitr" --wal-dir "$WAL_ARCHIVE_DIR" \
      --identity "$IDENT" --port "$PITR_PORT" --target-name t1
  local sock="$D/pitr/.s"
  [[ "$(PGHOST=$sock PGPORT=$PITR_PORT q -d drill -c "select count(*) from campaign where name='t1-marker'")" == 1 ]] && echo "  ok:   t1 row present" || { echo "  FAIL: t1 row missing"; return 1; }
  [[ "$(PGHOST=$sock PGPORT=$PITR_PORT q -d drill -c "select count(*) from campaign where name='t2-marker'")" == 0 ]] && echo "  ok:   t2 row ABSENT (recovered to t1)" || { echo "  FAIL: t2 row present"; return 1; }
  [[ "$(PGHOST=$sock PGPORT=$PITR_PORT q -d drill -c "select pg_is_in_recovery()")" == f ]] && echo "  ok:   pg_is_in_recovery() = false"
  [[ "$(PGHOST=$sock PGPORT=$PITR_PORT q -d drill -c "select timeline_id from pg_control_checkpoint()")" == 2 ]] && echo "  ok:   timeline advanced to 2"
  grep -E "recovery stopping|recovery target" "$D/pitr/restore.log" | head -n 3 | sed 's/^/        /'
}

drillC() {
  [[ -n $(find "$D/dest/logical" -name 'logical-*' 2>/dev/null) ]] || { seed drill base; run "$HERE/pg_logical_backup.sh"; }
  [[ -n $(find "$D/dest/base" -name 'base-*' 2>/dev/null) ]] || { run "$HERE/pg_base_backup.sh"; }
  local art; art="$(latest logical)"
  check "right key: decrypt + pg_restore --list" "$HERE/verify_backup.sh" --identity "$IDENT" "$art"
  must_fail "wrong key cannot decrypt" "$HERE/verify_backup.sh" --identity "$WRONG" "$art"
  check "logical --restore-test: scratch database restored, row counts + alembic revision match the manifest" \
    "$HERE/verify_backup.sh" --restore-test --identity "$IDENT" --tolerance-pct 0 "$art"
  check "base --restore-test: backup started on a private port, row counts match the manifest" \
    env "BACKUP_VERIFY_PORT=$((PORT + 6))" "$HERE/verify_backup.sh" --restore-test --identity "$IDENT" --tolerance-pct 0 "$(latest base)"
  [[ -z $(q -d postgres -c "select datname from pg_database where datname like 'verify_scratch_%'") ]] && echo "  ok:   the scratch database was dropped afterwards"
  local f bad=0
  while IFS= read -r f; do
    case "$f" in *.manifest.json|*.sha256) continue ;; esac
    head -c 21 -- "$f" | grep -q '^age-encryption.org/v1' || { echo "  FAIL: not age-encrypted: ${f#"$D"/}"; bad=1; }
    [[ "$(file_mode "$f")" == 600 ]] || { echo "  FAIL: mode $(file_mode "$f") on ${f#"$D"/}"; bad=1; }
  done < <(find "$D/dest" -type f)
  [[ $bad == 0 ]] && echo "  ok:   every artifact/WAL segment is age-encrypted and mode 0600 ($(find "$D/dest" -type f | wc -l) files)"
  [[ -z $(find "$D/dest" -type f \( -name '*.dump' -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.partial*' -o -name '.*' \)) ]] && echo "  ok:   no plaintext dump / tar / partial file left in the destination"
  [[ -z $(find "$D/stage" -mindepth 1) ]] && echo "  ok:   staging directory is empty (no decrypted material left)"
  [[ $bad == 0 ]]
}

flip_byte() { # flip_byte FILE OFFSET : XOR one byte with 0xFF in place
  local b; b="$(dd if="$1" bs=1 skip="$2" count=1 2>/dev/null | od -An -tu1 | tr -d ' ')"
  printf '%b' "\\x$(printf '%02x' $(( b ^ 255 )))" | dd of="$1" bs=1 seek="$2" conv=notrunc 2>/dev/null
}
drillD() {
  drillC >/dev/null
  local kind art copy
  for kind in logical base; do
    art="$(latest "$kind")"; copy="$D/corrupt-$kind"; mkdir -p "$copy"
    cp "$art" "$copy/${art##*/}"; cp "$art.manifest.json" "$copy/${art##*/}.manifest.json"
    flip_byte "$copy/${art##*/}" 200
    must_fail "$kind: one flipped byte fails the checksum" "$HERE/verify_backup.sh" "$copy/${art##*/}"
    # bypass the checksum on purpose: the encryption's own authentication must still catch it
    python3 - "$copy/${art##*/}.manifest.json" "$(sha256_of "$copy/${art##*/}")" <<'PY'
import json, sys
m = json.load(open(sys.argv[1])); m["sha256"] = sys.argv[2]
json.dump(m, open(sys.argv[1], "w"))
PY
    must_fail "$kind: with the checksum bypassed, decryption/structure check still fails" "$HERE/verify_backup.sh" --identity "$IDENT" "$copy/${art##*/}"
  done
}

drillE() {
  run "$HERE/pg_base_backup.sh" >/dev/null
  marker e1; wait_archived; marker e2; wait_archived
  marker e3; q -d drill -c "select pg_create_restore_point('e3')" >/dev/null; wait_archived
  local newest_base; newest_base="$(latest base)"
  run "$HERE/restore_pitr.sh" --base "$newest_base" --data-dir "$D/pitr-latest" --wal-dir "$WAL_ARCHIVE_DIR" \
      --identity "$IDENT" --port $((PORT + 4)) --target-latest
  [[ "$(PGHOST="$D/pitr-latest/.s" PGPORT=$((PORT + 4)) q -d drill -c "select count(*) from campaign where name in ('e1','e2','e3')")" == 3 ]] \
    && echo "  ok:   --target-latest recovered everything archived (e1, e2, e3 present)" || { echo "  FAIL: latest recovery incomplete"; return 1; }
  pg_ctl -D "$D/pitr-latest" -m fast stop >/dev/null
  local segs; mapfile -t segs < <(find "$WAL_ARCHIVE_DIR" -type f -name '*.age' -printf '%f\n' | grep -E '^[0-9A-F]{24}\.age$' | sort)
  local victim="${segs[-2]}"
  echo "  deleting one archived segment (the second newest): ${victim%.age}"
  rm -f -- "$WAL_ARCHIVE_DIR/$victim" "$WAL_ARCHIVE_DIR/${victim%.age}.sha256"
  must_fail "--target-latest refuses while the archive has a gap" "$HERE/restore_pitr.sh" --base "$newest_base" --data-dir "$D/pitr-gap" --wal-dir "$WAL_ARCHIVE_DIR" --identity "$IDENT" --port $((PORT + 5)) --target-latest
  [[ ! -e $D/pitr-gap ]] && echo "  ok:   nothing was extracted before the refusal"
  must_fail "PITR past the lost segment fails loudly" "$HERE/restore_pitr.sh" --base "$newest_base" --data-dir "$D/pitr-lost" --wal-dir "$WAL_ARCHIVE_DIR" --identity "$IDENT" --port $((PORT + 3)) --target-name e3 --timeout 60
  grep -E "ended before|FATAL" "$D/pitr-lost/restore.log" 2>/dev/null | head -n 2 | sed 's/^/        /'
  local out rc=0; out="$("$HERE/check_backup_freshness.sh" 2>/dev/null)" || rc=$?
  echo "        $out"
  [[ $rc == 2 && $out == *'"wal_gaps":{"status":"critical"'* ]] && echo "  ok:   check_backup_freshness.sh exits 2 and reports the WAL gap" || { echo "  FAIL: gap not reported (exit $rc)"; return 1; }
}

drillG() {
  createdb drill_g; alembic_in drill_g upgrade head >/dev/null 2>&1
  seed drill_g base
  PGDATABASE=drill_g run "$HERE/pg_logical_backup.sh" >/dev/null; sleep 1
  seed drill_g dnc +919800000211 +919800000212
  app drill_g "$PY" "$BACKEND/app/scripts/export_suppressions.py" --dest "$D/dnc" >/dev/null 2>&1 || \
    app drill_g env "BACKUP_ENV=production" "$PY" -m app.scripts.export_suppressions --dest "$D/dnc"
  local art copy; art="$(find "$D/dest/logical" -name 'logical-*.age' ! -name '*.json' | sort | tail -n 1)"
  copy="$(find "$D/dnc" -name 'dnc-*.age' | sort | tail -n 1)"
  PGDATABASE=drill_g run "$HERE/restore_logical.sh" --artifact "$art" --target-db drill_g_restored --identity "$IDENT" --tolerance-pct 0 >/dev/null
  local before; before="$(q -d drill_g_restored -c "select count(*) from suppression")"
  [[ $before == 2 ]] && echo "  ok:   restored database has only the 2 old DNC rows (2 newer entries are lost)"
  run app drill_g_restored "$PY" -m app.scripts.reconcile_suppressions --copy "$copy" --identity "$IDENT"
  [[ "$(q -d drill_g_restored -c "select count(*) from suppression")" == 2 ]] && echo "  ok:   dry run changed nothing"
  run app drill_g_restored "$PY" -m app.scripts.reconcile_suppressions --copy "$copy" --identity "$IDENT" --apply
  [[ "$(q -d drill_g_restored -c "select count(*) from suppression")" == 4 ]] && echo "  ok:   both lost numbers are back (4 rows)"
  [[ "$(q -d drill_g_restored -c "select count(*) from suppression where phone_number in ('+919800000201','+919800000202') and reason = 'drill seed'")" == 2 ]] && echo "  ok:   the original rows were not modified or deleted"
  [[ "$(q -d drill_g_restored -c "select count(*) from audit_log where action = 'suppression.reconciled'")" == 1 ]] && echo "  ok:   one counts-only audit event"
}

drillH() {
  createdb drill_h; alembic_in drill_h upgrade head >/dev/null 2>&1
  seed drill_h base; seed drill_h person +919800000401
  PGDATABASE=drill_h run "$HERE/pg_logical_backup.sh" >/dev/null; sleep 1
  seed drill_h erase +919800000401
  app drill_h "$PY" -m app.scripts.export_suppressions --dest "$D/dnc" >/dev/null
  app drill_h "$PY" -m app.scripts.export_suppressions --dest "$D/dnc" --kind erasure --now "$(date -u -d '+2 seconds' +%Y-%m-%dT%H:%M:%S+00:00)" >/dev/null
  local art dnc era
  art="$(find "$D/dest/logical" -name 'logical-*.age' | sort | tail -n 1)"
  dnc="$(find "$D/dnc" -name 'dnc-*.age' | sort | tail -n 1)"; era="$(find "$D/dnc" -name 'erasure-*.age' | sort | tail -n 1)"
  PGDATABASE=drill_h "$HERE/restore_logical.sh" --artifact "$art" --target-db drill_h_restored --identity "$IDENT" --tolerance-pct 0 >/dev/null
  [[ "$(q -d drill_h_restored -c "select count(*) from contact where phone_number = '+919800000401'")" == 1 ]] && echo "  ok:   the erased person is BACK in the older backup (contact + number present)"
  [[ "$(q -d drill_h_restored -c "select count(*) from erasure_log")" == 0 ]] && echo "  ok:   the erasure_log entry is lost too (it lived in the same database)"
  app drill_h_restored "$PY" -m app.scripts.reconcile_suppressions --copy "$dnc" --identity "$IDENT" --apply >/dev/null
  run app drill_h_restored "$PY" -m app.scripts.reconcile_suppressions --kind erasure --copy "$era" --identity "$IDENT" --apply
  run app drill_h_restored "$PY" -m app.scripts.reapply_erasures
  run app drill_h_restored "$PY" -m app.scripts.reapply_erasures --apply
  [[ "$(q -d drill_h_restored -c "select count(*) from contact where phone_number = '+919800000401'")" == 0 ]] && echo "  ok:   number scrubbed again"
  [[ "$(q -d drill_h_restored -c "select count(*) from contact where purged_at is not null and phone_number like 'purged:%'")" == 1 ]] && echo "  ok:   contact carries a purge placeholder"
  [[ "$(q -d drill_h_restored -c "select count(*) from conversation_message")" == 0 ]] && echo "  ok:   transcript turns deleted"
  [[ "$(q -d drill_h_restored -c "select count(*) from call_analysis where summary is not null")" == 0 ]] && echo "  ok:   analysis free text cleared"
  [[ "$(q -d drill_h_restored -c "select count(*) from suppression where phone_number = '+919800000401'")" == 1 ]] && echo "  ok:   the erased number is on the DNC list again"
}

drillI() {
  createdb drill_i; alembic_in drill_i upgrade a83d13f0c415 >/dev/null 2>&1
  q -d drill_i -c "insert into campaign (id, name, status) values (gen_random_uuid(), 'old-schema', 'active')" >/dev/null
  PGDATABASE=drill_i run "$HERE/pg_logical_backup.sh" >/dev/null
  local art; art="$(find "$D/dest/logical" -name 'logical-*.age' | sort | tail -n 1)"
  PGDATABASE=drill_i "$HERE/restore_logical.sh" --artifact "$art" --target-db drill_i_restored --identity "$IDENT" --tolerance-pct 0 >/dev/null
  echo "  restored revision: $(alembic_in drill_i_restored current 2>/dev/null | tail -n 1)"
  run alembic_in drill_i_restored upgrade head
  alembic_in drill_i_restored check 2>&1 | tail -n 1 | grep -q "No new upgrade operations" && echo "  ok:   alembic check clean after upgrading the restored old backup"
  [[ "$(q -d drill_i_restored -c "select count(*) from campaign where name='old-schema'")" == 1 ]] && echo "  ok:   old row survived the forward migration"
}

drillJ() {
  drillB >/dev/null
  local before after out
  before="$(PGHOST="$D/pitr/.s" PGPORT=$PITR_PORT db_fingerprint drill)"
  out="$(app pitr "$PY" -m app.scripts.post_restore_report)"
  after="$(PGHOST="$D/pitr/.s" PGPORT=$PITR_PORT db_fingerprint drill)"
  echo "$out" | sed 's/^/        /'
  [[ $before == "$after" ]] && echo "  ok:   no table changed (row counts + checksums identical before/after)"
  grep -q "Contacts in DIALING / RETRY_SCHEDULED: 2" <<<"$out" && grep -q "Call attempts in INITIATED: 1" <<<"$out" && grep -q "Analyses in PROCESSING: 1" <<<"$out" && echo "  ok:   the seeded stranded states are listed"
  ! grep -qE '\+91[0-9]{8,}' <<<"$out" && echo "  ok:   no phone number in the report"
}

main() {
  local want="${REST_ARGS[0]:-all}" ids results=0 id t0 rc
  [[ $want == all ]] && ids=(A B C D E G H I J) || ids=("$want")
  start_cluster
  echo "cluster: PostgreSQL $(postgres --version | awk '{print $3}') on a private socket, port $PORT (sandbox)"
  for id in "${ids[@]}"; do
    echo; echo "=== DRILL $id ==="; t0="$(date +%s)"; rc=0
    set +e; ( set -Eeuo pipefail; "drill$id" ); rc=$?; set -e
    if [[ $rc == 0 ]]; then echo "RESULT drill=$id status=PASS seconds=$(( $(date +%s) - t0 ))"
    else echo "RESULT drill=$id status=FAIL seconds=$(( $(date +%s) - t0 ))"; results=1; fi
  done
  return "$results"
}
main
