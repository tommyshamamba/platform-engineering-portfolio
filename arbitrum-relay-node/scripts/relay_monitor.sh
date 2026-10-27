#!/usr/bin/env bash
# Interactive troubleshooting loop for a local relay. Extracted from the heredoc in
# monitor_relay_wrapper.sh (Dec 2025); see README for the changes made.
set -u

RELAY_PORT="${RELAY_PORT:-9642}"
UPSTREAM_FEED="${FEED_INPUT_URL:-wss://arb1-feed.arbitrum.io/feed}"
INTERVAL="${INTERVAL:-10}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=== Nitro Relay Troubleshooting Monitor ==="
echo "Starting at: $(date)"

while true; do
  echo -e "\n$(date) - Checking..."

  # 1. Container state
  CID=$(docker ps -q --filter "name=arbitrum-relay" 2>/dev/null | head -n1)
  if [[ -z "$CID" ]]; then
    echo -e "${RED}✗ Relay container not running${NC}"
  else
    echo -e "${GREEN}✓ Relay container running ($CID)${NC}"
    MEM=$(docker stats --no-stream --format '{{.MemUsage}}' "$CID" 2>/dev/null || echo "unknown")
    echo -e "${YELLOW}  memory: ${MEM}${NC}"
  fi

  # 2. Port
  if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/${RELAY_PORT}" 2>/dev/null; then
    echo -e "${GREEN}✓ Port ${RELAY_PORT} is accepting connections${NC}"
  else
    echo -e "${RED}✗ Nothing listening on port ${RELAY_PORT}${NC}"
  fi

  # 3. WebSocket handshake (optional tool)
  if command -v websocat >/dev/null 2>&1; then
    echo -n "Testing WebSocket connection... "
    timeout 5 websocat -v "ws://127.0.0.1:${RELAY_PORT}" 2>&1 | grep -E "(Connected|error|failed)" | head -2
  fi

  # 4. Upstream reachability (HTTPS on the feed host; a WebSocket endpoint usually answers 4xx to plain GET)
  UP_HTTP="${UPSTREAM_FEED/wss:/https:}"
  echo -n "Upstream feed HTTP status: "
  timeout 5 curl -s -o /dev/null -w "%{http_code}\n" "$UP_HTTP" 2>/dev/null || echo "unreachable"

  # 5. Recent errors
  if [[ -n "$CID" ]]; then
    echo "Recent relay log errors:"
    docker logs --tail 50 "$CID" 2>&1 | grep -E "(ERROR|EOF|timed out)" | tail -3
  fi

  sleep "$INTERVAL"
done
