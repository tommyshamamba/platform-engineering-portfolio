#!/usr/bin/env bash
# Restart the relay container when its port stops accepting connections, or when a
# consumer log reports a stall. Adapted from run_relay.sh (Dec 2025); see README for changes.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

RELAY_HOST="${RELAY_HOST:-127.0.0.1}"
RELAY_PORT="${RELAY_PORT:-9642}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
RESTART_DELAY="${RESTART_DELAY:-5}"
CONSUMER_LOG="${CONSUMER_LOG:-}"
STALL_PATTERN="${STALL_PATTERN:-no heads for 90s}"
SERVICE="arbitrum-relay"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

check_relay() {
  # The relay is a WebSocket broadcaster; a TCP connect is the cheapest liveness probe.
  if ! timeout 3 bash -c "exec 3<>/dev/tcp/${RELAY_HOST}/${RELAY_PORT}" 2>/dev/null; then
    log "relay port ${RELAY_HOST}:${RELAY_PORT} not accepting connections"
    return 1
  fi
  if [[ -n "$CONSUMER_LOG" && -f "$CONSUMER_LOG" ]] && tail -n 50 "$CONSUMER_LOG" | grep -q "$STALL_PATTERN"; then
    log "consumer log reports a stall ('$STALL_PATTERN')"
    return 1
  fi
  return 0
}

restart_relay() {
  log "restarting ${SERVICE}"
  docker compose restart "$SERVICE"
  sleep "$RESTART_DELAY"
}

if [[ "${1:-}" == "--once" ]]; then
  check_relay
  exit $?
fi

while true; do
  if ! check_relay; then
    restart_relay
  fi
  sleep "$CHECK_INTERVAL"
done
