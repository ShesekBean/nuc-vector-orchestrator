#!/usr/bin/env bash
# OpenClaw Gateway (Signal) — run script
# Reconstructed from container inspect + ADE pattern
set -euo pipefail

CONTAINER_NAME="openclaw-gateway"
IMAGE="openclaw:signal"

# Stop and remove if exists
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

exec docker run --rm \
  --name "$CONTAINER_NAME" \
  -e "HOME=/home/node" \
  -e "TERM=xterm-256color" \
  -e "OPENCLAW_GATEWAY_TOKEN=fed3aea80e03410f8dae71c586049e85af3929b10d1f7a36508cabf05a5ec505" \
  -e "OPENCLAW_PREFER_PNPM=1" \
  -e "NODE_ENV=production" \
  -v "openclaw_signal_cli_data:/home/node/.local/share/signal-cli" \
  -v "/home/ophirsw/.openclaw:/home/node/.openclaw" \
  -v "/home/ophirsw/.openclaw/workspace:/home/node/.openclaw/workspace" \
  -p "127.0.0.1:18889:18789" \
  -p "127.0.0.1:18890:18790" \
  "$IMAGE" \
  node dist/index.js gateway --bind lan --port 18789
