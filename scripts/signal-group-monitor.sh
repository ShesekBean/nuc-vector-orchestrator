#!/bin/bash
# signal-group-monitor.sh — Catches Signal group messages to inbox file.
# No auto-reply — the orchestrator session reads the inbox and responds.
set -uo pipefail

STATE_DIR="/home/ophirsw/Documents/claude/nuc-vector-orchestrator/.claude/state"
INBOX_FILE="$STATE_DIR/signal-inbox.jsonl"
LOCKFILE="$STATE_DIR/group-monitor.lock"

BUILD_ORCH_GROUP="BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
SECURITY_ALERTS_GROUP="n5nNybjzxi33xFvofQPzvyvOMXBoBicOwet7UtLborQ="
BOT_CONTAINER="openclaw-gateway"
BOT_NUMBER="+14086469950"

mkdir -p "$STATE_DIR"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Already running"; exit 1; }

while true; do
    sg docker -c "docker exec $BOT_CONTAINER curl -sfN http://127.0.0.1:8080/api/v1/events" 2>/dev/null | while IFS= read -r line; do
        [[ "$line" == data:* ]] || continue
        json="${line#data:}"
        python3 -c "
import json, sys, time
try:
    event = json.loads(r'''${json}''')
except: sys.exit(0)
envelope = event.get('envelope', {})
data_msg = envelope.get('dataMessage', {})
group_info = data_msg.get('groupInfo', {})
group_id = group_info.get('groupId', '')
source = envelope.get('sourceNumber', '') or envelope.get('source', '')
message = data_msg.get('message', '')
WATCHED = {'$BUILD_ORCH_GROUP': 'build-orchestrator', '$SECURITY_ALERTS_GROUP': 'security-alerts'}
if group_id not in WATCHED or not message: sys.exit(0)
is_bot = (source == '$BOT_NUMBER')
entry = {'ts': int(time.time()*1000), 'group': WATCHED[group_id], 'gid': group_id, 'from': 'bot' if is_bot else source, 'msg': message, 'replied': True if is_bot else False}
with open('$INBOX_FILE', 'a') as f: f.write(json.dumps(entry) + '\n')
if not is_bot: print(f'[{WATCHED[group_id]}] {message}', flush=True)
" 2>/dev/null
    done
    sleep 5
done
