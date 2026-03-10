#!/bin/bash
# inbox-responder.sh — Listens for Signal group messages and auto-replies via Claude
# Combines SSE monitoring + response in one process. No separate monitor needed.
set -uo pipefail

STATE_DIR="/home/ophirsw/Documents/claude/nuc-orchestrator/.claude/state"
REPO_DIR="/home/ophirsw/Documents/claude/nuc-orchestrator"
INBOX_FILE="$STATE_DIR/signal-inbox.jsonl"
LOCKFILE="$STATE_DIR/inbox-responder.lock"
LOG_FILE="$STATE_DIR/inbox-responder.log"

BUILD_ORCH_GROUP="BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
BOT_CONTAINER="openclaw-gateway"
OPHIR_NUMBER="+14084758230"
RESPONDER_TIMEOUT=600

mkdir -p "$STATE_DIR"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Already running"; exit 1; }

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

send_reply() {
    local msg="$1"
    local tmp
    tmp=$(mktemp /tmp/sig-reply-XXXXXX.json)
    python3 -c "
import json, sys
print(json.dumps({
    'jsonrpc': '2.0',
    'method': 'send',
    'params': {'groupId': '$BUILD_ORCH_GROUP', 'message': sys.argv[1]},
    'id': 1
}))" "$msg" > "$tmp"
    sg docker -c "docker cp '$tmp' '$BOT_CONTAINER:/tmp/sig-reply.json'" 2>/dev/null
    sg docker -c "docker exec '$BOT_CONTAINER' curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' -d @/tmp/sig-reply.json" 2>/dev/null
    rm -f "$tmp"
}

process_message() {
    local message="$1"
    local timestamp="$2"
    log "Processing message from Ophir: $message"

    # Log to inbox
    python3 -c "
import json, time
entry = {'ts': $timestamp, 'group': 'build-orchestrator', 'gid': '$BUILD_ORCH_GROUP', 'from': '$OPHIR_NUMBER', 'msg': '''${message//\'/\'\\\'\'}''', 'replied': False}
with open('$INBOX_FILE', 'a') as f:
    f.write(json.dumps(entry) + '\n')
" 2>/dev/null || true

    # Build orchestrator prompt
    local prompt
    prompt=$(cat <<PROMPT
You are the Orchestrator for Project Shon. Read .claude/CLAUDE.md for full context.

Ophir sent this message on the build-orchestrator Signal group:

"${message}"

INSTRUCTIONS:
1. Run through Coach quality gate: evaluate clarity, risks, conflicts, dependencies
   - If concerns: reply on Signal with a COACH prefix and STOP
   - If no concerns: proceed silently
2. Take the appropriate action:
   - Work instructions: create a GitHub Issue with label assigned:pm
   - Question: answer directly
   - Feedback on existing issue: comment on that issue
   - Approval (yes/go ahead): check recent blocker:needs-human issues and act
   - Greeting: reply with brief status update of open issues
3. Reply to Ophir on Signal. To send a message:
   python3 -c "import json, sys; print(json.dumps({'jsonrpc':'2.0','method':'send','params':{'groupId':'${BUILD_ORCH_GROUP}','message': sys.argv[1]},'id':1}))" "YOUR_MESSAGE" > /tmp/sig-reply.json
   sg docker -c "docker cp /tmp/sig-reply.json ${BOT_CONTAINER}:/tmp/sig-reply.json"
   sg docker -c "docker exec ${BOT_CONTAINER} curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' -d @/tmp/sig-reply.json"
4. Always prefix reply with the appropriate emoji prefix
5. Keep replies concise for Signal

RULES:
- NEVER read .env or secrets
- NEVER run sudo
- NEVER modify .md files
- NEVER restart OpenClaw containers
PROMPT
)

    cd "$REPO_DIR" || return
    local result=0
    env -u CLAUDECODE timeout "$RESPONDER_TIMEOUT" claude --dangerously-skip-permissions -p "$prompt" 2>&1 | tee -a "$LOG_FILE" || result=$?

    # Mark as replied in inbox
    python3 -c "
import json
lines = []
with open('$INBOX_FILE') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        m = json.loads(line)
        if m.get('ts') == $timestamp and not m.get('replied'):
            m['replied'] = True
        lines.append(json.dumps(m))
with open('$INBOX_FILE', 'w') as f:
    f.write('\n'.join(lines) + '\n')
" 2>/dev/null || true

    if (( result != 0 )); then
        log "Orchestrator session failed (exit code: $result)"
        send_reply "Orchestrator: Sorry, I hit an error processing your message. Will retry next cycle."
    else
        log "Message processed successfully"
    fi
}

log "=== Inbox responder starting ==="

# Main loop: connect to SSE, process events
while true; do
    log "Connecting to SSE stream..."
    sg docker -c "docker exec $BOT_CONTAINER curl -sfN http://127.0.0.1:8080/api/v1/events" 2>/dev/null | while IFS= read -r line; do
        [[ "$line" == data:* ]] || continue
        json="${line#data:}"

        # Extract message details
        result=$(python3 -c "
import json, sys
try:
    event = json.loads(r'''${json}''')
except:
    sys.exit(0)
envelope = event.get('envelope', {})
data_msg = envelope.get('dataMessage', {})
group_info = data_msg.get('groupInfo', {})
group_id = group_info.get('groupId', '')
source = envelope.get('sourceNumber', '') or envelope.get('source', '')
message = data_msg.get('message', '')
timestamp = data_msg.get('timestamp', 0)
# Only process: from Ophir, on build-orchestrator, with actual text
if group_id != '$BUILD_ORCH_GROUP': sys.exit(0)
if source != '$OPHIR_NUMBER': sys.exit(0)
if not message: sys.exit(0)
print(f'{timestamp}|{message}')
" 2>/dev/null) || continue

        [[ -z "$result" ]] && continue

        ts="${result%%|*}"
        msg="${result#*|}"
        log "Received from Ophir: $msg"
        process_message "$msg" "$ts"
    done

    log "SSE connection dropped, reconnecting in 5s..."
    sleep 5
done
