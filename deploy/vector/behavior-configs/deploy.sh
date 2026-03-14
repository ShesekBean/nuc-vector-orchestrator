#!/usr/bin/env bash
# Deploy stripped behavior configs to Vector.
#
# Makes Vector sit still by default (Wait-only HighLevelAI, 24h QuietMode).
# Wake word, SDK commands, and voice commands still work — they interrupt Wait.
#
# Usage: bash deploy/vector/behavior-configs/deploy.sh

set -euo pipefail

VECTOR_HOST="${VECTOR_HOST:-vector}"
BEHAVIOR_BASE="/anki/data/assets/cozmo_resources/config/engine/behaviorComponent/behaviors/victorBehaviorTree"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying sit-still behavior configs to Vector ==="

# Remount rootfs read-write
echo "Remounting rootfs rw..."
ssh "$VECTOR_HOST" 'mount -o remount,rw /'

# Back up originals (skip if backup already exists)
echo "Backing up originals..."
ssh "$VECTOR_HOST" "
  [ -f ${BEHAVIOR_BASE}/highLevelAI.json.pre-sitstill ] || \
    cp ${BEHAVIOR_BASE}/highLevelAI.json ${BEHAVIOR_BASE}/highLevelAI.json.pre-sitstill
  [ -f ${BEHAVIOR_BASE}/quietMode/quietMode.json.pre-sitstill ] || \
    cp ${BEHAVIOR_BASE}/quietMode/quietMode.json ${BEHAVIOR_BASE}/quietMode/quietMode.json.pre-sitstill
"

# Deploy new configs
echo "Deploying highLevelAI.json (Wait-only)..."
scp "${SCRIPT_DIR}/highLevelAI.json" "${VECTOR_HOST}:${BEHAVIOR_BASE}/highLevelAI.json"

echo "Deploying quietMode.json (24h active time)..."
scp "${SCRIPT_DIR}/quietMode.json" "${VECTOR_HOST}:${BEHAVIOR_BASE}/quietMode/quietMode.json"

# Sync and reboot
echo "Syncing and rebooting Vector..."
ssh "$VECTOR_HOST" 'sync && reboot'

echo "=== Done. Vector will reboot and sit still by default. ==="
echo "Wait ~60s for reboot, then verify with: ssh vector 'cat ${BEHAVIOR_BASE}/highLevelAI.json'"
