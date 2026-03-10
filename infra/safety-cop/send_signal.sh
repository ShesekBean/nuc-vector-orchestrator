#!/usr/bin/env bash
set -euo pipefail

MESSAGE="$1"
TARGET_TYPE="${2:-recipient}"   # "recipient" or "group"
TARGET_VALUE="${3:-+14084758230}"
BOT_CONTAINER="openclaw-gateway"
TMPFILE=$(mktemp /tmp/signal_cop_XXXXXX.json)

python3 -c "
import json, sys
msg, ttype, tval = sys.argv[1], sys.argv[2], sys.argv[3]
params = {'message': msg}
if ttype == 'group':
    params['groupId'] = tval
else:
    params['recipient'] = tval
payload = {'jsonrpc': '2.0', 'method': 'send', 'params': params, 'id': 1}
print(json.dumps(payload))
" "$MESSAGE" "$TARGET_TYPE" "$TARGET_VALUE" > "$TMPFILE"

docker cp "$TMPFILE" "${BOT_CONTAINER}:/tmp/sig-cop.json"
docker exec "$BOT_CONTAINER" \
    curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc \
    -H "Content-Type: application/json" \
    -d @/tmp/sig-cop.json

rm -f "$TMPFILE"
