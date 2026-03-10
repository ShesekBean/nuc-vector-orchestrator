#!/bin/bash
# start-all.sh — Start ALL Project Shon services on NUC
#
# Starts systemd services and verifies they're running.
# Does NOT touch OpenClaw/Docker containers (those run independently).
#
# Usage: bash scripts/start-all.sh           # start everything
#        bash scripts/start-all.sh --dry-run  # show what would be started

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[start-all] $*"; }

cmd() {
    if [[ "$DRY_RUN" == "true" ]]; then
        log "WOULD RUN: $*"
    else
        # shellcheck disable=SC2294
        eval "$@"  # eval is intentional; commands use shell features (||, 2>/dev/null)
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
log "══ NUC ══"
# ══════════════════════════════════════════════════════════════════════════════

# Pull latest code
log "Pulling latest NUC code..."
cmd "cd /home/ophirsw/Documents/claude/nuc-vector-orchestrator && git pull --ff-only 2>/dev/null || true"

# Install service file from repo (source of truth)
REPO_DIR="/home/ophirsw/Documents/claude/nuc-vector-orchestrator"
SYSTEMD_DIR="$HOME/.config/systemd/user"
if [[ -f "$REPO_DIR/infra/systemd/nuc-agent-loop.service" ]]; then
    if ! diff -q "$REPO_DIR/infra/systemd/nuc-agent-loop.service" "$SYSTEMD_DIR/nuc-agent-loop.service" >/dev/null 2>&1; then
        log "Installing updated service file..."
        cmd "cp $REPO_DIR/infra/systemd/nuc-agent-loop.service $SYSTEMD_DIR/nuc-agent-loop.service"
        cmd "systemctl --user daemon-reload"
    fi
fi

# Start services
log "Starting NUC agent-loop..."
cmd "systemctl --user start nuc-agent-loop.service"

log "Starting NUC watchdog timer..."
cmd "systemctl --user start nuc-agent-loop-watchdog.timer 2>/dev/null || true"

# Start signal monitor (background, not a systemd service)
if ! pgrep -f "signal-group-monitor\.sh" >/dev/null 2>&1; then
    log "Starting signal-group-monitor..."
    cmd "nohup bash /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/signal-group-monitor.sh \
         >/dev/null 2>&1 &"
else
    log "signal-group-monitor already running"
fi

# NUC agent-loop dispatches everything — no Vector agent-loop needed.
# Vector communicates via gRPC, not SSH.

# ══════════════════════════════════════════════════════════════════════════════
log "══ Verify ══"
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$DRY_RUN" == "false" ]]; then
    sleep 3

    log "NUC:"
    if systemctl --user is-active --quiet nuc-agent-loop.service 2>/dev/null; then
        nuc_pid=$(systemctl --user show nuc-agent-loop.service -p MainPID --value 2>/dev/null)
        log "  agent-loop: running (PID $nuc_pid) ✓"
    else
        log "  agent-loop: NOT running ✗"
    fi

    if pgrep -f "signal-group-monitor\.sh" >/dev/null 2>&1; then
        log "  signal-monitor: running ✓"
    else
        log "  signal-monitor: NOT running ✗"
    fi
fi

log "Done."
