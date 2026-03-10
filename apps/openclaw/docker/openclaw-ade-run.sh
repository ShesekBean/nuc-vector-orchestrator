#!/usr/bin/env bash
# OpenClaw ADE (Work Bot) — run script
# Reconstructed from container inspect
set -euo pipefail

CONTAINER_NAME="openclaw-ade"
IMAGE="openclaw:signal"

# Stop and remove if exists
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

exec docker run --rm \
  --name "$CONTAINER_NAME" \
  -e "HOME=/home/node" \
  -e "TERM=xterm-256color" \
  -e "OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_ADE_GATEWAY_TOKEN:?Set OPENCLAW_ADE_GATEWAY_TOKEN}" \
  -e "OPENCLAW_PREFER_PNPM=1" \
  -e "NODE_ENV=production" \
  -v "openclaw_ade_signal_cli_data:/home/node/.local/share/signal-cli" \
  -v "/home/ophirsw/.openclaw-ade:/home/node/.openclaw" \
  -v "/home/ophirsw/.openclaw-ade/workspace:/home/node/.openclaw/workspace" \
  -p "127.0.0.1:18891:18789" \
  -p "127.0.0.1:18892:18790" \
  "$IMAGE" \
  node dist/index.js gateway --bind lan --port 18789
