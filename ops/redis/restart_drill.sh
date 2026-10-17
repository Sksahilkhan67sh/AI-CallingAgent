#!/usr/bin/env bash
# P1-3 drill: restart Redis and prove the stream, its consumer group, the pending entries and a
# sorted set survive, that AOF/everysec is on, and that unauthenticated access is refused.
#
#   MODE=graceful ./ops/redis/restart_drill.sh   # docker compose restart (SIGTERM; AOF flushed)
#   MODE=crash    ./ops/redis/restart_drill.sh   # SIGKILL; everysec may lose <= ~1 s of writes,
#                                                # so the drill waits 2 s after seeding first
#
# Needs REDIS_PASSWORD in the environment (or a .env next to docker-compose.yml). Touches only
# drill:* keys. Exit 0 = PASS, non-zero = FAIL with the reason on stderr.
#
# Overridable for non-compose targets (the CI-less local check in docs uses these):
#   REDIS_CLI         command that talks to the AUTHENTICATED Redis
#   REDIS_CLI_NOAUTH  command that talks to it WITHOUT credentials
#   RESTART_CMD       command that restarts it (must return once the process is restarted)
set -euo pipefail

MODE="${MODE:-graceful}"
case "$MODE" in graceful | crash) ;; *) echo "MODE must be graceful or crash" >&2; exit 2 ;; esac

COMPOSE="${COMPOSE:-docker compose}"
if [[ -z "${REDIS_CLI:-}" ]]; then
  [[ -n "${REDIS_PASSWORD:-}" ]] || { [[ -f .env ]] && set -a && . ./.env && set +a; }
  [[ -n "${REDIS_PASSWORD:-}" ]] || { echo "REDIS_PASSWORD is not set" >&2; exit 2; }
  command -v docker >/dev/null || { echo "docker not found (set REDIS_CLI/RESTART_CMD for another target)" >&2; exit 2; }
fi
REDIS_CLI="${REDIS_CLI:-$COMPOSE exec -T redis redis-cli}"
REDIS_CLI_NOAUTH="${REDIS_CLI_NOAUTH:-$COMPOSE exec -T -e REDISCLI_AUTH= redis redis-cli}"
if [[ -z "${RESTART_CMD:-}" ]]; then
  if [[ "$MODE" == crash ]]; then
    RESTART_CMD="$COMPOSE kill -s KILL redis && $COMPOSE up -d redis"
  else
    RESTART_CMD="$COMPOSE restart redis"
  fi
fi

fail() { echo "FAIL: $*" >&2; exit 1; }
r() { $REDIS_CLI "$@" | tr -d '\r'; }

echo "== config"
[[ "$(r config get appendonly | sed -n 2p)" == yes ]] || fail "appendonly is not yes"
[[ "$(r config get appendfsync | sed -n 2p)" == everysec ]] || fail "appendfsync is not everysec"
noauth="$($REDIS_CLI_NOAUTH ping 2>&1 | tr -d '\r' || true)"
[[ "$noauth" == *NOAUTH* ]] || fail "unauthenticated PING was not refused (got: $noauth)"
echo "appendonly=yes appendfsync=everysec unauthenticated access refused"

echo "== seed (drill:* only)"
r del drill:stream drill:zset >/dev/null
r xgroup create drill:stream drill-g '$' mkstream >/dev/null
for i in 1 2 3; do r xadd drill:stream '*' job "drill-$i" >/dev/null; done
r xreadgroup group drill-g drill-c1 count 1 streams drill:stream '>' >/dev/null  # 1 stays pending
r zadd drill:zset 1 member-a >/dev/null
before_len="$(r xlen drill:stream)"
before_pending="$(r xpending drill:stream drill-g | sed -n 1p)"
before_prod="$(r xlen calls:outbound)"
[[ "$before_len" == 3 && "$before_pending" == 1 ]] || fail "seed unexpected: len=$before_len pending=$before_pending"
echo "stream len=$before_len pending=$before_pending (calls:outbound len=$before_prod, informational)"

[[ "$MODE" == crash ]] && { echo "waiting 2 s so the everysec fsync has run"; sleep 2; }

echo "== restart ($MODE)"
bash -c "$RESTART_CMD"
for _ in $(seq 1 60); do
  [[ "$(r ping 2>/dev/null || true)" == PONG ]] && break
  sleep 1
done
[[ "$(r ping 2>/dev/null || true)" == PONG ]] || fail "Redis did not come back within 60 s"

echo "== verify"
after_len="$(r xlen drill:stream)"
after_pending="$(r xpending drill:stream drill-g | sed -n 1p)"
[[ "$after_len" == "$before_len" ]] || fail "stream length changed: $before_len -> $after_len"
r xinfo groups drill:stream | grep -qx 'drill-g' || fail "consumer group drill-g is gone"
[[ "$after_pending" == "$before_pending" ]] || fail "pending entries changed: $before_pending -> $after_pending"
r xinfo consumers drill:stream drill-g | grep -qx 'drill-c1' || fail "consumer drill-c1 is gone"
[[ "$(r zcard drill:zset)" == 1 ]] || fail "sorted set lost its member"
[[ "$(r xlen calls:outbound)" == "$before_prod" ]] || fail "calls:outbound length changed across the restart"

r del drill:stream drill:zset >/dev/null
echo "PASS ($MODE): stream, group, consumer, pending entries and sorted set survived"
