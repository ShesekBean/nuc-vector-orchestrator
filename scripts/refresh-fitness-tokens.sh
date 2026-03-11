#!/usr/bin/env bash
# Refresh Strava and Withings OAuth tokens in OpenClaw fitness-log.json.
# Runs the Python script inside the openclaw-gateway container.
# Installed as a systemd timer (every 2 hours).
set -euo pipefail

CONTAINER="openclaw-gateway"
SCRIPT="/home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/refresh-fitness-tokens.py"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "[fitness-token-refresh] ERROR: Container $CONTAINER not running"
    exit 1
fi

# Copy script into container and run it
docker cp "$SCRIPT" "$CONTAINER:/tmp/refresh-fitness-tokens.py"
docker exec "$CONTAINER" python3 /tmp/refresh-fitness-tokens.py
