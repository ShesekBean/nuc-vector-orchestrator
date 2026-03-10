#!/usr/bin/env bash
# backup-openclaw.sh — Auto-commit OpenClaw changes to git every 6 hours
#
# 1. Copies live files from ~/.openclaw → apps/openclaw/ in the repo
# 2. If anything changed, commits and pushes to the monorepo
#
# Runs as a systemd timer (openclaw-backup.timer).
#
# Usage:
#   bash scripts/backup-openclaw.sh          # run once
#   bash scripts/backup-openclaw.sh --dry-run # show what would be committed

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[backup-openclaw] $(date '+%Y-%m-%d %H:%M') $*"; }

cd "$REPO_DIR"

# Pull latest first to avoid conflicts
git pull --ff-only 2>/dev/null || log "WARNING: git pull failed (will try to push anyway)"

# Copy live files from ~/.openclaw → repo (reverse sync)
bash scripts/sync-openclaw.sh --reverse

# Stage changes in OpenClaw dirs
git add apps/openclaw/

if git diff --cached --quiet; then
    log "No changes to back up"
    exit 0
fi

# Show what changed
CHANGED=$(git diff --cached --stat)
log "Changes detected:"
echo "$CHANGED"

if $DRY_RUN; then
    log "DRY RUN — would commit the above"
    git reset HEAD -- apps/openclaw/ >/dev/null
    exit 0
fi

# Commit and push
git commit -m "Auto-backup: OpenClaw skills, cron, memory, workspace

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"

git push && log "Pushed to remote" || log "WARNING: push failed"
