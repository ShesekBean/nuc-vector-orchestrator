#!/usr/bin/env bash
# travel-mode.sh — Network-aware security for OpenClaw skills
#
# Detects WiFi SSID and enables/disables sensitive skills based on
# whether the NUC is on the home network or traveling.
#
# Usage:
#   bash scripts/travel-mode.sh status    — show current mode and skill states
#   bash scripts/travel-mode.sh check     — auto-detect SSID and toggle mode
#   bash scripts/travel-mode.sh enable    — force travel mode ON (disable sensitive skills)
#   bash scripts/travel-mode.sh disable   — force travel mode OFF (enable all skills)
#   bash scripts/travel-mode.sh unlock    — re-enable sensitive skills (PIN verified externally)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF_FILE="$REPO_DIR/config/travel-mode.conf"

# ── Load config ──
if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: Config not found: $CONF_FILE"
    exit 1
fi
# shellcheck source=../config/travel-mode.conf
source "$CONF_FILE"

# ── Paths ──
WORKSPACE_SKILLS="$HOME/.openclaw/workspace/skills"
SANDBOX_DIR="$HOME/.openclaw/sandboxes"
REPO_SKILLS="$REPO_DIR/apps/openclaw/skills"

mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

# ── Helpers ──
log() { echo "[travel-mode] $*"; }

get_current_ssid() {
    # Returns the SSID of the active WiFi connection, or empty if not on WiFi
    # Uses || true to avoid pipefail exit when nmcli is unavailable (e.g. CI)
    nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes:' | head -1 | cut -d: -f2- || true
}

is_home_network() {
    local ssid="$1"
    if [[ -z "$ssid" ]]; then
        return 1  # No WiFi = not home (could be ethernet, handled separately)
    fi
    IFS=',' read -ra home_list <<< "$HOME_SSIDS"
    for home_ssid in "${home_list[@]}"; do
        if [[ "$ssid" == "$home_ssid" ]]; then
            return 0
        fi
    done
    return 1
}

# Check if ethernet is connected (fallback: treat wired as home)
is_wired_connection() {
    nmcli -t -f type,state dev 2>/dev/null | grep -q '^ethernet:connected$' || return 1
}

get_sandbox_skills_dirs() {
    # Returns all sandbox skill directories (there may be multiple sandboxes)
    local dirs=()
    if [[ -d "$SANDBOX_DIR" ]]; then
        for sb in "$SANDBOX_DIR"/agent-main-*/; do
            if [[ -d "${sb}skills" ]]; then
                dirs+=("${sb}skills")
            fi
        done
    fi
    echo "${dirs[@]}"
}

disable_skill() {
    local skill_name="$1"
    local disabled=false

    # Disable in workspace
    local ws_skill="$WORKSPACE_SKILLS/$skill_name/SKILL.md"
    if [[ -f "$ws_skill" ]]; then
        mv "$ws_skill" "${ws_skill}.disabled"
        log "Disabled in workspace: $skill_name"
        disabled=true
    fi

    # Disable in all sandboxes
    for sb_skills in $(get_sandbox_skills_dirs); do
        local sb_skill="$sb_skills/$skill_name/SKILL.md"
        if [[ -f "$sb_skill" ]]; then
            mv "$sb_skill" "${sb_skill}.disabled"
            log "Disabled in sandbox: $skill_name"
            disabled=true
        fi
    done

    # Disable in repo (for consistency)
    local repo_skill="$REPO_SKILLS/$skill_name/SKILL.md"
    if [[ -f "$repo_skill" ]]; then
        mv "$repo_skill" "${repo_skill}.disabled"
        disabled=true
    fi

    if ! $disabled; then
        log "Skill already disabled or not found: $skill_name"
    fi
}

enable_skill() {
    local skill_name="$1"
    local enabled=false

    # Enable in workspace
    local ws_skill="$WORKSPACE_SKILLS/$skill_name/SKILL.md.disabled"
    if [[ -f "$ws_skill" ]]; then
        mv "$ws_skill" "${ws_skill%.disabled}"
        log "Enabled in workspace: $skill_name"
        enabled=true
    fi

    # Enable in all sandboxes
    for sb_skills in $(get_sandbox_skills_dirs); do
        local sb_skill="$sb_skills/$skill_name/SKILL.md.disabled"
        if [[ -f "$sb_skill" ]]; then
            mv "$sb_skill" "${sb_skill%.disabled}"
            log "Enabled in sandbox: $skill_name"
            enabled=true
        fi
    done

    # Enable in repo (for consistency)
    local repo_skill="$REPO_SKILLS/$skill_name/SKILL.md.disabled"
    if [[ -f "$repo_skill" ]]; then
        mv "$repo_skill" "${repo_skill%.disabled}"
        enabled=true
    fi

    if ! $enabled; then
        log "Skill already enabled or not found: $skill_name"
    fi
}

is_skill_disabled() {
    local skill_name="$1"
    # Check sandbox first (that's what OpenClaw reads from)
    for sb_skills in $(get_sandbox_skills_dirs); do
        if [[ -f "$sb_skills/$skill_name/SKILL.md.disabled" ]]; then
            return 0
        fi
    done
    # Fall back to workspace check
    if [[ -f "$WORKSPACE_SKILLS/$skill_name/SKILL.md.disabled" ]]; then
        return 0
    fi
    return 1
}

get_mode() {
    # Travel mode is ON if any sensitive skill is disabled
    IFS=',' read -ra sensitive_list <<< "$SENSITIVE_SKILLS"
    for skill in "${sensitive_list[@]}"; do
        if is_skill_disabled "$skill"; then
            echo "travel"
            return
        fi
    done
    echo "home"
}

clear_session() {
    rm -f "${SESSION_FILE}"
    log "Session cleared (PIN unlock revoked)"
}

has_valid_session() {
    if [[ ! -f "${SESSION_FILE}" ]]; then
        return 1
    fi
    # Session is valid if it exists and matches current SSID
    local session_ssid
    session_ssid=$(TRAVEL_SESSION_FILE="${SESSION_FILE}" python3 -c "
import json, os
try:
    with open(os.environ['TRAVEL_SESSION_FILE']) as f:
        data = json.load(f)
    print(data.get('ssid', ''))
except Exception:
    print('')
")
    local current_ssid
    current_ssid=$(get_current_ssid)
    # Session is valid only if SSID hasn't changed
    if [[ "$session_ssid" == "$current_ssid" && -n "$session_ssid" ]]; then
        return 0
    fi
    # SSID changed — invalidate session
    clear_session
    return 1
}

send_signal_notification() {
    local message="$1"
    local event_type="${2:-general}"
    if [[ -x "$SCRIPT_DIR/pgm-signal-gate.sh" || -f "$SCRIPT_DIR/pgm-signal-gate.sh" ]]; then
        bash "$SCRIPT_DIR/pgm-signal-gate.sh" "$event_type" 0 "$message" 2>/dev/null || true
    else
        log "Signal notification skipped (pgm-signal-gate.sh not found)"
    fi
}

# ── Commands ──
cmd_status() {
    local ssid
    ssid=$(get_current_ssid)
    local mode
    mode=$(get_mode)
    local is_home="no"
    if is_home_network "$ssid"; then
        is_home="yes"
    elif [[ -z "$ssid" ]] && is_wired_connection; then
        is_home="yes (wired)"
    fi

    echo "=== Travel Mode Status ==="
    echo "Current SSID:    ${ssid:-<not on WiFi>}"
    echo "Home network:    $is_home"
    echo "Mode:            $mode"
    echo "PIN unlock:      $(has_valid_session && echo 'active' || echo 'inactive')"
    echo ""
    echo "Sensitive skills:"
    IFS=',' read -ra sensitive_list <<< "$SENSITIVE_SKILLS"
    for skill in "${sensitive_list[@]}"; do
        if is_skill_disabled "$skill"; then
            echo "  ✗ $skill (DISABLED)"
        else
            echo "  ✓ $skill (enabled)"
        fi
    done
    echo ""
    echo "Safe skills (always enabled):"
    IFS=',' read -ra safe_list <<< "$SAFE_SKILLS"
    for skill in "${safe_list[@]}"; do
        echo "  ✓ $skill"
    done
}

cmd_check() {
    local ssid
    ssid=$(get_current_ssid)
    local current_mode
    current_mode=$(get_mode)

    if is_home_network "$ssid"; then
        if [[ "$current_mode" == "travel" ]]; then
            log "Home network detected ($ssid) — disabling travel mode"
            cmd_disable_internal
            send_signal_notification "🏠 Home mode activated — all skills enabled" "board-status"
        else
            log "Already in home mode on $ssid"
        fi
    elif [[ -z "$ssid" ]] && is_wired_connection; then
        if [[ "$current_mode" == "travel" ]]; then
            log "Wired connection detected — treating as home network"
            cmd_disable_internal
            send_signal_notification "🏠 Home mode activated (wired) — all skills enabled" "board-status"
        else
            log "Already in home mode (wired)"
        fi
    else
        if [[ "$current_mode" == "home" ]]; then
            log "Unknown network detected (${ssid:-<no WiFi>}) — enabling travel mode"
            cmd_enable_internal
            send_signal_notification "🔒 Travel mode activated (${ssid:-no WiFi}) — sensitive skills disabled. Send 'unlock <PIN>' to re-enable." "board-status"
        else
            log "Already in travel mode on ${ssid:-<no WiFi>}"
        fi
    fi
}

cmd_enable_internal() {
    # Enable travel mode (disable sensitive skills)
    IFS=',' read -ra sensitive_list <<< "$SENSITIVE_SKILLS"
    for skill in "${sensitive_list[@]}"; do
        disable_skill "$skill"
    done
    clear_session
    log "Travel mode ENABLED — sensitive skills disabled"
}

cmd_enable() {
    cmd_enable_internal
    send_signal_notification "🔒 Travel mode activated (manual) — sensitive skills disabled. Send 'unlock <PIN>' to re-enable." "board-status"
}

cmd_disable_internal() {
    # Disable travel mode (re-enable all skills)
    IFS=',' read -ra sensitive_list <<< "$SENSITIVE_SKILLS"
    for skill in "${sensitive_list[@]}"; do
        enable_skill "$skill"
    done
    clear_session
    log "Travel mode DISABLED — all skills enabled"
}

cmd_disable() {
    cmd_disable_internal
    send_signal_notification "🏠 Home mode activated — all skills enabled" "board-status"
}

cmd_unlock() {
    # Called after PIN verification succeeds — re-enable sensitive skills for this session
    local ssid
    ssid=$(get_current_ssid)

    IFS=',' read -ra sensitive_list <<< "$SENSITIVE_SKILLS"
    for skill in "${sensitive_list[@]}"; do
        enable_skill "$skill"
    done

    # Save session (tied to current SSID) — pass SSID via env to avoid injection
    TRAVEL_SSID="$ssid" TRAVEL_SESSION_FILE="${SESSION_FILE}" python3 -c "
import json, os, time
data = {'ssid': os.environ.get('TRAVEL_SSID', ''), 'unlocked_at': time.time()}
with open(os.environ['TRAVEL_SESSION_FILE'], 'w') as f:
    json.dump(data, f)
"
    chmod 600 "${SESSION_FILE}"
    log "Skills unlocked for this session (SSID: ${ssid:-<unknown>})"
}

# ── Main ──
case "${1:-status}" in
    status)  cmd_status ;;
    check)   cmd_check ;;
    enable)  cmd_enable ;;
    disable) cmd_disable ;;
    unlock)  cmd_unlock ;;
    *)
        echo "Usage: bash scripts/travel-mode.sh {status|check|enable|disable|unlock}"
        exit 1
        ;;
esac
