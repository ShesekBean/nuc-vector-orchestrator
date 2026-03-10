#!/bin/bash
# openclaw-gateway-run.sh — foreground runner for systemd
# Do NOT run this directly; use start-gateway.sh for manual starts.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment, expand ~ in paths
set -a
# shellcheck source=/dev/null
source .env
set +a
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR/#\~/$HOME}"
OPENCLAW_WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR/#\~/$HOME}"

# Ensure Docker network exists
docker network inspect openclaw_net >/dev/null 2>&1 \
  || docker network create openclaw_net

# Remove any stale container with the same name
docker rm -f openclaw-gateway 2>/dev/null || true

# Run in foreground (no -d) so systemd tracks the process
exec docker run \
  --name openclaw-gateway \
  --rm \
  --init \
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
