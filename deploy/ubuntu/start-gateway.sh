#!/bin/bash
# start-gateway.sh — start (or restart) the OpenClaw + Signal gateway
# Usage: sg docker -c "bash start-gateway.sh"
#
# Uses flock to prevent the sg double-execution race condition.

set -e
exec 9>/tmp/openclaw-start.lock
flock -n 9 || exit 0   # only one instance runs; second silently exits

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment
set -a
# shellcheck source=/dev/null
source .env
set +a

echo "[openclaw] Stopping any existing gateway..."
docker rm -f openclaw-gateway 2>/dev/null || true
docker rm -f openclaw-openclaw-gateway-1 2>/dev/null || true

echo "[openclaw] Ensuring network exists..."
docker network inspect openclaw_net >/dev/null 2>&1 \
  || docker network create openclaw_net

echo "[openclaw] Starting gateway..."
docker run -d \
  --name openclaw-gateway \
  --init \
  --restart unless-stopped \
  --network openclaw_net \
  -p "127.0.0.1:${OPENCLAW_GATEWAY_PORT:-18889}:18789" \
  -p "127.0.0.1:${OPENCLAW_BRIDGE_PORT:-18890}:18790" \
  --dns 172.20.0.53 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --memory 1.5g \
  --cpus 2.0 \
  -e HOME=/home/node \
  -e TERM=xterm-256color \
  -e "OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_GATEWAY_TOKEN}" \
  -v "${OPENCLAW_CONFIG_DIR}:/home/node/.openclaw" \
  -v "${OPENCLAW_WORKSPACE_DIR}:/home/node/.openclaw/workspace" \
  -v "openclaw_signal_cli_data:/home/node/.local/share/signal-cli" \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 \
  "${OPENCLAW_IMAGE:-openclaw:signal}" \
  node dist/index.js gateway --bind "${OPENCLAW_GATEWAY_BIND:-lan}" --port 18789

echo "[openclaw] Gateway started. Waiting for Signal to connect..."
sleep 6

echo "[openclaw] Recent logs:"
docker exec openclaw-gateway \
  tail -20 "/tmp/openclaw/openclaw-$(date +%Y-%m-%d).log" 2>/dev/null \
  | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        obj = json.loads(line)
        ts = obj.get('time','')[:19].replace('T',' ')
        msg = obj.get('1', obj.get('0',''))
        sub = obj.get('0','')
        if isinstance(sub, dict): sub = sub.get('subsystem','openclaw')
        print(f'{ts} [{sub}] {msg}')
    except:
        print(line.rstrip())
" 2>/dev/null || docker logs --tail=15 openclaw-gateway 2>&1

echo ""
echo "[openclaw] Gateway is running on port ${OPENCLAW_GATEWAY_PORT:-18889}"
echo "[openclaw] Signal bot: +14086469950"

# Hold lock briefly so the second sg invocation can't race
sleep 2
