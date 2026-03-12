#!/usr/bin/env bash
# Serve vector-web-setup locally.
# Requires Chrome (Web Bluetooth API).
# Usage: bash deploy/vector/web-setup/serve.sh [port]
#
# This is a local mirror of https://vector-web-setup.anki.bot
# The Bluetooth pairing, WiFi config, and settings steps work fully offline.
# Account auth (Sign In) requires wire-pod's Stratus or DDL cloud — skip if using wire-pod.
set -euo pipefail

PORT="${1:-8000}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Serving vector-web-setup at http://localhost:$PORT"
echo "Open in Chrome (Web Bluetooth required)"
python3 -m http.server "$PORT" --directory "$DIR"
