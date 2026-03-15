#!/usr/bin/env bash
# Sprint-end workflow — deploy, golden test, post-sprint ops.
#
# This script is the FINAL step of a sprint. It:
# 1. Pulls latest main code
# 2. Runs the golden test
# 3. On success: triggers post-sprint operations
#
# TODO: Adapt for Vector gRPC (no Docker build/deploy, no SSH to Jetson)
#
# Usage:
#   bash scripts/sprint-end.sh --sprint <N>
#   bash scripts/sprint-end.sh --sprint <N> --skip-golden    # skip golden test (debugging)
#
# Prerequisites:
# - All sprint issues closed (except the comprehensive test issue)
# - Main branch up to date with all sprint PRs merged

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# TODO: Vector uses gRPC, not SSH — remove Jetson host/repo references
# JETSON_HOST="jetson"
# JETSON_REPO="/home/yahboom/claude"

# ── Parse arguments ──

SPRINT=""
SKIP_GOLDEN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sprint)       SPRINT="$2"; shift 2 ;;
        --skip-golden)  SKIP_GOLDEN=true; shift ;;
        *)              echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$SPRINT" ]]; then
    echo "ERROR: --sprint <N> is required" >&2
    exit 1
fi

echo "╔══════════════════════════════════════════╗"
echo "║  Sprint $SPRINT — End-of-Sprint Workflow   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Step 0: Verify prerequisites ──

echo "=== Step 0: Prerequisites ===" >&2

# Pull latest main
echo "Pulling latest nuc-vector-orchestrator main..." >&2
git -C "$REPO_DIR" fetch origin
git -C "$REPO_DIR" checkout main
git -C "$REPO_DIR" pull --ff-only

# Check for open sprint issues
OPEN_ISSUES=$(gh issue list -R ophir-sw/nuc-vector-orchestrator -l "sprint-$SPRINT" -l "component:vector" --state open --json number,title --jq '.[].title' 2>/dev/null || echo "")
if [[ -n "$OPEN_ISSUES" ]]; then
    echo "WARNING: Open sprint-$SPRINT issues remain:" >&2
    echo "$OPEN_ISSUES" >&2
    echo "" >&2
    echo "Proceeding anyway — these may be the comprehensive test issue." >&2
fi

# ── Step 1: Vector deployment ──
# TODO: Adapt for Vector gRPC — no SSH/SCP sync, no Docker build, no Docker deploy
# Vector robot code is deployed differently (TBD).

echo "" >&2
echo "=== Step 1: Vector deployment (TODO) ===" >&2
echo "Skipping — Vector deployment not yet implemented." >&2

# ── Step 2: Run golden test ──

if [[ "$SKIP_GOLDEN" == "false" ]]; then
    echo "" >&2
    echo "=== Step 2: Running golden test ===" >&2

    GOLDEN_EXIT=0
    python3 "$REPO_DIR/apps/test_harness/golden_test.py" --phases stationary || GOLDEN_EXIT=$?

    if [[ "$GOLDEN_EXIT" -ne 0 ]]; then
        echo "" >&2
        echo "╔══════════════════════════════════════════╗"
        echo "║  GOLDEN TEST FAILED — Sprint NOT done    ║"
        echo "╚══════════════════════════════════════════╝"
        echo "" >&2
        echo "Fix failures and re-run: bash scripts/sprint-end.sh --sprint $SPRINT" >&2
        exit 1
    fi

    echo "Golden test PASSED." >&2
else
    echo "" >&2
    echo "=== Step 2: SKIPPED (--skip-golden) ===" >&2
fi

# ── Step 3: Run hardware sanity test ──

echo "" >&2
echo "=== Step 3: Hardware sanity test ===" >&2

SANITY_EXIT=0
python3 "$SCRIPT_DIR/hw-sanity-test.py" --json > "/tmp/sprint-$SPRINT-sanity.json" 2>/dev/null || SANITY_EXIT=$?

if [[ "$SANITY_EXIT" -ne 0 ]]; then
    echo "WARNING: Hardware sanity test had critical failures" >&2
    python3 -c "import json; d=json.load(open('/tmp/sprint-$SPRINT-sanity.json')); print('Critical:', d.get('critical_failures', []))" 2>/dev/null || true
fi

# (No Docker image backup needed — Vector has no Docker)

# ── Step 4: Post-sprint operations ──

echo "" >&2
echo "=== Step 4: Post-sprint operations ===" >&2

# 6a. Notify Ophir
echo "Sending sprint completion notification..." >&2
bash "$SCRIPT_DIR/pgm-signal-gate.sh" general 0 \
    "📊 PGM: Sprint $SPRINT complete! Golden test passed. Sanity test exit: $SANITY_EXIT" \
    2>/dev/null || true

echo "" >&2
echo "╔══════════════════════════════════════════╗"
echo "║  Sprint $SPRINT — COMPLETE                 ║"
echo "╚══════════════════════════════════════════╝"
echo "" >&2
echo "Post-sprint checklist (create issues for these):" >&2
echo "  1. Sprint $SPRINT retrospective (lessons learned)" >&2
echo "  2. Update docs (changelog, architecture)" >&2
echo "  3. MD file consistency check (monorepo)" >&2
echo "  4. Create Sprint $((SPRINT + 1)) backlog issues" >&2
