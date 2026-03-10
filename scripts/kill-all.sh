#!/bin/bash
# kill-all.sh — Stop ALL Project Shon processes on NUC
#
# Stops systemd services + kills any orphan processes.
# Does NOT touch OpenClaw/Docker containers (those are sacred).
#
# Run from a separate terminal.
#
# Usage: bash scripts/kill-all.sh           # kill everything
#        bash scripts/kill-all.sh --dry-run  # show what would be killed

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[kill-all] $*"; }

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

# Stop systemd services
log "Stopping NUC systemd services..."
cmd "systemctl --user stop nuc-agent-loop.service 2>/dev/null || true"
cmd "systemctl --user stop nuc-agent-loop-watchdog.timer 2>/dev/null || true"
cmd "systemctl --user stop nuc-agent-loop-watchdog.service 2>/dev/null || true"

# Kill any orphan processes
log "Killing NUC orphan processes..."
for pattern in \
    "[a]gent-loop\.sh" \
    "[s]ignal-group-monitor\.sh" \
    "[i]nbox-responder\.sh" \
    "[s]print-advance\.sh" \
    "[b]oard-check\.sh" \
    "[c]laude.*--dangerously-skip-permissions" \
    "[t]ail -f /tmp/claude.*tasks"; do
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        pid=$(echo "$line" | awk '{print $2}')
        desc=$(echo "$line" | awk '{for(i=11;i<=NF;i++) printf "%s ",$i; print ""}')
        if [[ "$DRY_RUN" == "true" ]]; then
            log "WOULD KILL: PID $pid — $desc"
        else
            log "Killing PID $pid — $desc"
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done < <(ps aux | grep "$pattern" 2>/dev/null || true)
done

# Wait for TERM signals to take effect, then force kill stragglers
if [[ "$DRY_RUN" == "false" ]]; then
    sleep 2
    for pattern in "[a]gent-loop\.sh" "[c]laude.*--dangerously-skip-permissions"; do
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            pid=$(echo "$line" | awk '{print $2}')
            log "Force killing NUC PID $pid"
            kill -9 "$pid" 2>/dev/null || true
        done < <(ps aux | grep "$pattern" 2>/dev/null || true)
    done
fi

# No Vector agent-loop to stop — NUC handles everything.

# ══════════════════════════════════════════════════════════════════════════════
log "══ Verify ══"
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$DRY_RUN" == "false" ]]; then
    sleep 1
    log "NUC:"
    nuc_remaining=$(ps aux | grep -E "agent-loop\.sh|signal-group-monitor|claude.*skip-permissions" | grep -v grep | wc -l || echo 0)
    if [[ "$nuc_remaining" -eq 0 ]]; then
        log "  All clean ✓"
    else
        log "  WARNING: $nuc_remaining process(es) still running"
        ps aux | grep -E "agent-loop\.sh|signal-group-monitor|claude.*skip-permissions" | grep -v grep || true
    fi
fi

log "Done."
