#!/usr/bin/env bash
# Apply wire-pod LLM response timeout fix (Issue #167)
#
# Problem: Vector shows "lost connection to cloud" animation when LLM
# takes >10s to respond, because the intent graph gRPC stream isn't
# answered until the first LLM sentence arrives.
#
# Fix: Send IntentPass immediately for non-KG requests before the LLM
# call, matching the KG path pattern.
#
# Usage: bash deploy/wirepod/apply-llm-timeout-fix.sh
#        Then rebuild wire-pod: cd ~/Documents/claude/wire-pod/chipper && sudo ./start.sh

set -euo pipefail

WIREPOD_DIR="${HOME}/Documents/claude/wire-pod"
PATCH_FILE="$(dirname "$0")/fix-llm-response-timeout.patch"

if [ ! -d "$WIREPOD_DIR" ]; then
    echo "Error: wire-pod not found at $WIREPOD_DIR"
    exit 1
fi

if [ ! -f "$PATCH_FILE" ]; then
    echo "Error: patch file not found at $PATCH_FILE"
    exit 1
fi

echo "Applying LLM response timeout fix to wire-pod..."
cd "$WIREPOD_DIR"

# Check if patch is already applied
if git diff --quiet -- chipper/pkg/wirepod/ttr/kgsim.go 2>/dev/null; then
    git apply "$PATCH_FILE"
    echo "Patch applied successfully."
else
    echo "wire-pod already has local changes in kgsim.go."
    echo "Patch may already be applied. Check manually."
    exit 1
fi

echo ""
echo "Next steps:"
echo "  1. Restart wire-pod: cd ~/Documents/claude/wire-pod/chipper && sudo ./start.sh"
echo "  2. Test: ask Vector a tool-heavy question (email, daily brief)"
echo "  3. Verify: no 'lost connection' animation during 15-30s LLM response"
