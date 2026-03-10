#!/usr/bin/env bash
# sync-openclaw.sh — Sync OpenClaw workspace files between repo and ~/.openclaw
#
# Copies files from the repo (source of truth) to ~/.openclaw so the Docker
# container can read them via its bind mount. Symlinks don't work because
# Docker bind mounts can't follow symlinks to paths outside the container.
#
# What stays LOCAL (not in repo):
#   ~/.openclaw/openclaw.json  — has secrets (API keys, tokens)
#
# Usage:
#   bash scripts/sync-openclaw.sh              # copy repo → ~/.openclaw
#   bash scripts/sync-openclaw.sh --check      # verify files match repo
#   bash scripts/sync-openclaw.sh --reverse    # copy ~/.openclaw → repo (for backup)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCLAW_DIR="$HOME/.openclaw"
REPO_OPENCLAW="$REPO_DIR/apps/openclaw"

DIRS=(skills cron memory)
DIR_TARGETS=("$OPENCLAW_DIR/workspace/skills" "$OPENCLAW_DIR/cron" "$OPENCLAW_DIR/workspace/memory")
DIR_SOURCES=("$REPO_OPENCLAW/skills" "$REPO_OPENCLAW/cron" "$REPO_OPENCLAW/workspace/memory")
FILES=(SOUL.md AGENTS.md USER.md IDENTITY.md TOOLS.md HEARTBEAT.md)

log() { echo "[sync-openclaw] $*"; }

if [[ "${1:-}" == "--check" ]]; then
    ok=true
    for i in "${!DIRS[@]}"; do
        target="${DIR_TARGETS[$i]}"
        src="${DIR_SOURCES[$i]}"
        if [[ -d "$target" && ! -L "$target" ]]; then
            log "✓ $target exists (real directory)"
        elif [[ -L "$target" ]]; then
            log "⚠ $target is a symlink (breaks Docker!) — run sync to fix"
            ok=false
        else
            log "✗ $target missing"
            ok=false
        fi
    done
    for f in "${FILES[@]}"; do
        target="$OPENCLAW_DIR/workspace/$f"
        if [[ -f "$target" && ! -L "$target" ]]; then
            log "✓ $target exists"
        elif [[ -L "$target" ]]; then
            log "⚠ $target is a symlink (breaks Docker!) — run sync to fix"
            ok=false
        else
            log "✗ $target missing"
            ok=false
        fi
    done
    $ok && log "All files OK" || { log "Some files missing — run without --check to fix"; exit 1; }
    exit 0
fi

if [[ "${1:-}" == "--reverse" ]]; then
    # Copy from ~/.openclaw → repo (backup direction)
    for i in "${!DIRS[@]}"; do
        src="${DIR_TARGETS[$i]}"
        dest="${DIR_SOURCES[$i]}"
        if [[ -d "$src" && ! -L "$src" ]]; then
            mkdir -p "$dest"
            rsync -a --delete "$src/" "$dest/"
            log "← ${DIRS[$i]} backed up to repo"
        fi
    done
    for f in "${FILES[@]}"; do
        src="$OPENCLAW_DIR/workspace/$f"
        dest="$REPO_OPENCLAW/workspace/$f"
        if [[ -f "$src" && ! -L "$src" ]]; then
            cp "$src" "$dest"
            log "← $f backed up to repo"
        fi
    done
    log "Done. Live files copied to repo for git backup."
    exit 0
fi

# ── Copy repo → ~/.openclaw ──
for i in "${!DIRS[@]}"; do
    src="${DIR_SOURCES[$i]}"
    target="${DIR_TARGETS[$i]}"
    name="${DIRS[$i]}"
    mkdir -p "$src"
    # Remove symlinks (legacy) or create target dir
    if [[ -L "$target" ]]; then
        rm "$target"
    fi
    mkdir -p "$target"
    rsync -a --delete "$src/" "$target/"
    log "$name ← $src"
done

# ── Workspace root files ──
for f in "${FILES[@]}"; do
    src="$REPO_OPENCLAW/workspace/$f"
    target="$OPENCLAW_DIR/workspace/$f"
    if [[ -f "$src" ]]; then
        # Remove symlinks (legacy)
        if [[ -L "$target" ]]; then
            rm "$target"
        fi
        cp "$src" "$target"
        log "$f ← $src"
    fi
done

log "Done. Repo files copied to ~/.openclaw for Docker container."
log "Config stays local at ~/.openclaw/openclaw.json (has secrets)"
