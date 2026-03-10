#!/bin/bash
# pgm-signal-gate.sh — Rate-limited Signal sender for PGM
#
# Enforces dedup so PGM (stateless LLM) can't spam Ophir.
#
# Usage: bash scripts/pgm-signal-gate.sh <event_type> <issue_id> "<message>"
#
# Notification policy (set by Ophir):
#   closed       — once per issue, never repeat
#   stuck        — once per 24h per issue (flat, no backoff)
#   blocker      — once per 24h per issue (flat, no backoff)
#   physical     — once per issue, never repeat (appears in general updates only)
#   idle         — once per idle window (resets when work found)
#   general      — 3x/day at 6am, 12pm, 6pm only (+ 1h dedup window)
#   board-status — once per change (5min dedup window)
#   board        — once per change (5min dedup window)
#   md-drift     — once per 24h (flat)
#   premature    — once per 24h per issue (flat)
#
#   pipeline     — SUPPRESSED (Ophir not interested)
#   ci           — SUPPRESSED (Ophir not interested)
#
# TSV format: EVENT_KEY\tTIMESTAMP\tCOUNT
# Reset: agent-loop.sh deletes entries when issue state changes

set -euo pipefail

EVENT_TYPE="${1:-general}"
ISSUE_ID="${2:-0}"
MESSAGE="${3:-}"

if [[ -z "$MESSAGE" ]]; then
    echo "Usage: bash scripts/pgm-signal-gate.sh <event_type> <issue_id> \"<message>\""
    exit 1
fi

# ── Suppressed event types — silently drop ──
case "$EVENT_TYPE" in
    pipeline|ci)
        echo "GATE: Suppressed '$EVENT_TYPE' — Ophir not interested in this event type"
        exit 0
        ;;
esac

# ── Scheduled event types — only send during allowed windows ──
if [[ "$EVENT_TYPE" == "general" ]]; then
    HOUR=$(date +%-H)
    # Allowed windows: 6:00-6:59, 12:00-12:59, 18:00-18:59
    if [[ "$HOUR" -ne 6 && "$HOUR" -ne 12 && "$HOUR" -ne 18 ]]; then
        echo "GATE: Skipped 'general' — outside scheduled hours (6am/12pm/6pm), current hour: ${HOUR}"
        exit 0
    fi
fi

STATE_DIR="${REPO_DIR:-$HOME/Documents/claude/nuc-orchestrator}/.claude/state"
SENT_LOG="$STATE_DIR/pgm-signal-sent.tsv"
mkdir -p "$STATE_DIR"
touch "$SENT_LOG"

ALERT_GROUP_ID="BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
BOT_CONTAINER="openclaw-gateway"

# ── Cooldown policy ──
# "once" types use a huge cooldown so they fire once and stop
# "flat" types use fixed cooldown (no exponential backoff)
# "dedup" types use a short window just to prevent double-sends
case "$EVENT_TYPE" in
    closed)       BASE_COOLDOWN=999999 ;;  # once per issue, never repeat
    stuck)        BASE_COOLDOWN=86400 ;;   # 24h flat
    blocker)      BASE_COOLDOWN=86400 ;;   # 24h flat
    physical)     BASE_COOLDOWN=999999 ;;  # once per issue, never repeat
    idle)         BASE_COOLDOWN=999999 ;;  # once per idle window
    general)      BASE_COOLDOWN=3600 ;;    # 1h dedup (time-window enforced above)
    board-status) BASE_COOLDOWN=300 ;;     # 5min dedup
    board)        BASE_COOLDOWN=300 ;;     # 5min dedup
    md-drift)     BASE_COOLDOWN=86400 ;;   # 24h flat
    premature)    BASE_COOLDOWN=86400 ;;   # 24h flat
    *)            BASE_COOLDOWN=28800 ;;   # 8h default
esac

EVENT_KEY="${EVENT_TYPE}-${ISSUE_ID}"
NOW=$(date +%s)

# Check last send time and count for this event key
LAST_SENT=0
SEND_COUNT=0
if grep -q "^${EVENT_KEY}	" "$SENT_LOG" 2>/dev/null; then
    LAST_LINE=$(grep "^${EVENT_KEY}	" "$SENT_LOG" | tail -1)
    LAST_SENT=$(echo "$LAST_LINE" | cut -f2)
    COUNT_FIELD=$(echo "$LAST_LINE" | cut -f3)
    if [[ -n "$COUNT_FIELD" && "$COUNT_FIELD" =~ ^[0-9]+$ ]]; then
        SEND_COUNT=$COUNT_FIELD
    else
        SEND_COUNT=1
    fi
fi

# Calculate cooldown — flat (no exponential backoff)
if [[ "$SEND_COUNT" -gt 0 ]]; then
    COOLDOWN=$BASE_COOLDOWN
else
    COOLDOWN=0  # first send — no cooldown
fi

ELAPSED=$(( NOW - LAST_SENT ))
if (( ELAPSED < COOLDOWN )); then
    REMAINING=$(( COOLDOWN - ELAPSED ))
    echo "GATE: Skipped '$EVENT_KEY' — sent ${ELAPSED}s ago, cooldown ${COOLDOWN}s (${REMAINING}s remaining)"
    exit 0
fi

# Send the Signal message (use python3 for safe JSON encoding of arbitrary message text)
python3 -c "
import json, sys
msg = sys.stdin.read()
payload = {'jsonrpc':'2.0','method':'send','params':{'groupId':'${ALERT_GROUP_ID}','message':msg},'id':1}
print(json.dumps(payload))
" <<< "$MESSAGE" > /tmp/sig-msg.json
sg docker -c "docker cp /tmp/sig-msg.json ${BOT_CONTAINER}:/tmp/sig-msg.json"
sg docker -c "docker exec ${BOT_CONTAINER} curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' -d @/tmp/sig-msg.json"

# Update send count and timestamp
NEW_COUNT=$((SEND_COUNT + 1))
if grep -q "^${EVENT_KEY}	" "$SENT_LOG" 2>/dev/null; then
    sed -i "s/^${EVENT_KEY}	.*/${EVENT_KEY}	${NOW}	${NEW_COUNT}/" "$SENT_LOG"
else
    printf '%s\t%s\t%s\n' "${EVENT_KEY}" "${NOW}" "${NEW_COUNT}" >> "$SENT_LOG"
fi

echo "GATE: Sent '$EVENT_KEY' — send #${NEW_COUNT}, next cooldown: ${BASE_COOLDOWN}s"
