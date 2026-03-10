#!/bin/bash
# openclaw-monitor.sh — outbound DNS + inbound Signal security monitor
#
# OUTBOUND: fires when the container tries to reach a domain not on the DNS allowlist.
#           Sends a Signal alert; reply "#ALLOW# <domain>" to auto-approve.
# INBOUND:  fires when someone not on the Signal allowlist tries to message Shon.
# APPROVALS: subscribes to signal-cli SSE stream (sse-watcher.py) for real-time replies.
#
# Alerts are sent to ALERT_NUMBER via Signal.
# Same event is suppressed for DEDUP_TTL seconds to prevent alert floods.

set -euo pipefail

# ── One instance only ─────────────────────────────────────────────────────────
exec 9>/tmp/openclaw-monitor.lock
flock -n 9 || exit 0

# ── Config ────────────────────────────────────────────────────────────────────
ALERT_NUMBER="+14084758230"
ALERT_GROUP_ID="n5nNybjzxi33xFvofQPzvyvOMXBoBicOwet7UtLborQ="
BOT_CONTAINER="openclaw-gateway"
DNS_CONTAINER="openclaw-dns"
DNSMASQ_CONF="/home/ophirsw/openclaw-dns/dnsmasq.conf"
SSE_WATCHER="/home/ophirsw/openclaw-dns/sse-watcher.py"
DEDUP_DIR="/tmp/openclaw-monitor-dedup"
PENDING_DIR="/tmp/openclaw-pending-approval"
DEDUP_TTL=3600
START_DATE=$(date +%Y-%m-%d)

mkdir -p "$DEDUP_DIR" "$PENDING_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Dedup ─────────────────────────────────────────────────────────────────────
should_alert() {
    local safe now last
    safe=$(printf '%s' "$1" | tr -cs 'a-zA-Z0-9_-' '_')
    local stamp="$DEDUP_DIR/$safe"
    now=$(date +%s)
    if [[ -f "$stamp" ]]; then
        last=$(cat "$stamp")
        (( now - last < DEDUP_TTL )) && return 1
    fi
    echo "$now" > "$stamp"
    return 0
}

# ── Send Signal alert ─────────────────────────────────────────────────────────
send_signal() {
    local msg="$1"
    local tmp
    tmp=$(mktemp /tmp/sig-XXXXXX.json)

    python3 - "$ALERT_GROUP_ID" "$msg" > "$tmp" <<'PY'
import json, sys, base64
group_id = sys.argv[1]
print(json.dumps({
    "jsonrpc": "2.0",
    "method":  "send",
    "params":  {"groupId": group_id, "message": sys.argv[2]},
    "id": 1
}))
PY

    if docker cp "$tmp" "$BOT_CONTAINER:/tmp/sig-alert.json" 2>/dev/null \
    && docker exec "$BOT_CONTAINER" \
           curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc \
           -H 'Content-Type: application/json' \
           -d @/tmp/sig-alert.json 2>/dev/null \
       | python3 -c "import sys,json; r=json.load(sys.stdin); sys.exit(0 if 'result' in r else 1)"; then
        log "  alert delivered"
    else
        log "  WARN: alert failed (container not ready?)"
    fi

    rm -f "$tmp"
}

# ── OUTBOUND monitor ──────────────────────────────────────────────────────────
watch_outbound() {
    log "OUTBOUND: tailing $DNS_CONTAINER"
    docker logs --tail 0 -f "$DNS_CONTAINER" 2>&1 \
    | while IFS= read -r line; do
        [[ "$line" =~ config[[:space:]]([^[:space:]]+)[[:space:]].*NXDOMAIN ]] || continue
        local domain="${BASH_REMATCH[1]}"

        should_alert "out_$domain" || continue
        log "OUTBOUND BLOCKED: $domain"

        # Save pending approval for sse-watcher.py to match against
        echo "$(date +%s)" > "$PENDING_DIR/$domain"

        send_signal "[OUTBOUND BLOCKED] $(date '+%Y-%m-%d %H:%M')
Domain: $domain
The gateway tried to reach a domain not on the DNS allowlist.

Reply with the message below to allow it:" || true
        send_signal "#ALLOW# $domain" || true
    done
}

# ── INBOUND monitor ───────────────────────────────────────────────────────────
watch_inbound() {
    local logfile="/tmp/openclaw/openclaw-${START_DATE}.log"
    log "INBOUND: tailing $logfile"

    docker exec "$BOT_CONTAINER" tail -n 0 -F "$logfile" 2>/dev/null \
    | while IFS= read -r line; do
        local text
        text=$(printf '%s' "$line" | python3 -c "
import sys, json
try:
    o = json.loads(sys.stdin.read())
    print(' '.join(str(v) for v in [o.get('0',''), o.get('1','')] if v).lower())
except:
    pass
" 2>/dev/null) || continue
        [[ -z "$text" ]] && continue

        echo "$text" | grep -qiE '(not allowed|reject|allowlist|dmpolicy|blocked)' || continue

        local sender
        sender=$(printf '%s' "$line" | grep -oP '\+\d{10,}' | head -1) || sender="unknown"

        should_alert "in_$sender" || continue
        log "INBOUND BLOCKED: $sender"
        send_signal "[INBOUND BLOCKED] $(date '+%Y-%m-%d %H:%M')
Sender: $sender
Someone NOT on the Signal allowlist tried to message Shon.
To allow: add to ~/.openclaw/openclaw.json → channels.signal.allowFrom" || true
    done
}

# ── APPROVAL monitor ──────────────────────────────────────────────────────────
# Pipes signal-cli's SSE stream into sse-watcher.py (a standalone script).
# Using a separate file avoids the bash heredoc-vs-pipe stdin conflict.
# Multiple SSE subscribers are supported; the gateway keeps its own connection.
# Inner loop reconnects automatically if the stream drops.
watch_approvals() {
    while true; do
        log "APPROVALS: subscribing to signal-cli SSE stream"
        docker exec "$BOT_CONTAINER" \
            curl -sN http://127.0.0.1:8080/api/v1/events 2>/dev/null \
        | python3 "$SSE_WATCHER" \
            "$ALERT_NUMBER" "$PENDING_DIR" "$DNSMASQ_CONF" \
            "$BOT_CONTAINER" "$DNS_CONTAINER" \
        || true
        log "APPROVALS: SSE stream ended, reconnecting in 5s..."
        sleep 5
    done
}

# ── Midnight restart ──────────────────────────────────────────────────────────
watchdog_midnight() {
    while true; do
        sleep 60
        [[ "$(date +%Y-%m-%d)" != "$START_DATE" ]] && {
            log "midnight: date changed, exiting for systemd restart"
            exit 0
        }
    done
}

# ── Main ──────────────────────────────────────────────────────────────────────
log "openclaw-monitor started (PID $$)"
log "  outbound  : DNS blocks on $DNS_CONTAINER"
log "  inbound   : Signal rejections in gateway log"
log "  approvals : #ALLOW# replies via signal-cli SSE stream"
log "  alerts → Security Alerts group  (dedup TTL: ${DEDUP_TTL}s)"

watch_outbound  & OUTBOUND_PID=$!
watch_inbound   & INBOUND_PID=$!
watch_approvals & APPROVALS_PID=$!
watchdog_midnight &

while true; do
    sleep 10
    kill -0 "$OUTBOUND_PID"  2>/dev/null || { log "outbound died — restarting service";  break; }
    kill -0 "$INBOUND_PID"   2>/dev/null || { log "inbound died — restarting service";   break; }
    kill -0 "$APPROVALS_PID" 2>/dev/null || { log "approvals died — restarting service"; break; }
done

kill "$OUTBOUND_PID" "$INBOUND_PID" "$APPROVALS_PID" 2>/dev/null || true
exit 1
