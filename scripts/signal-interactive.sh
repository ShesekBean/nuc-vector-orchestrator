#!/bin/bash
# signal-interactive.sh — Reusable interactive Signal checkpoint library
#
# Source this from any script that needs interactive Signal checkpoints:
#   source scripts/signal-interactive.sh
#
# Provides:
#   sig_send <message>             — Send a Signal message (no wait)
#   sig_checkpoint <message> [timeout]  — Send + wait for any reply, returns reply text
#   sig_verify <message> [timeout]      — Send + wait for pass/fail, returns "pass"/"fail"/"timeout"
#   sig_wait_reply [timeout]       — Wait for next Signal reply (no send)
#   SIG_LAST_REPLY                 — Full text of last reply
#   SIG_LAST_COMMENT               — Extracted comment after pass/fail keyword
#
# All functions are non-blocking if SIG_INTERACTIVE=false (auto-returns "ok"/"pass").

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

SIG_INTERACTIVE="${SIG_INTERACTIVE:-true}"
SIG_DEFAULT_TIMEOUT="${SIG_DEFAULT_TIMEOUT:-600}"  # 10 min
SIG_INBOX_FILE="${SIG_INBOX_FILE:-.claude/state/signal-inbox.jsonl}"
SIG_GROUP_ID="${SIG_GROUP_ID:-BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4=}"
export SIG_LAST_REPLY=""
SIG_LAST_COMMENT=""

# ── Internal ──────────────────────────────────────────────────────────────────

_sig_log() { echo "[signal-interactive] $(date +%H:%M:%S) $*"; }

# ── sig_send — Fire-and-forget Signal message ────────────────────────────────

sig_send() {
    local message="$1"
    python3 -c "
import json, sys
msg = sys.stdin.read()
payload = {'jsonrpc':'2.0','method':'send','params':{'groupId':'$SIG_GROUP_ID','message':msg},'id':1}
print(json.dumps(payload))
" <<< "$message" > /tmp/sig-msg.json
    sg docker -c "docker cp /tmp/sig-msg.json openclaw-gateway:/tmp/sig-msg.json" 2>/dev/null
    sg docker -c "docker exec openclaw-gateway curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' -d @/tmp/sig-msg.json" 2>/dev/null || true
}

# ── sig_wait_reply — Poll inbox for new message from Ophir ───────────────────

sig_wait_reply() {
    local timeout="${1:-$SIG_DEFAULT_TIMEOUT}"
    local pre_start="${2:-}"  # Optional: pre-captured epoch timestamp

    if [[ "$SIG_INTERACTIVE" != "true" ]]; then
        _sig_log "Non-interactive mode: auto-returning 'ok'"
        SIG_LAST_REPLY="ok"
        SIG_LAST_COMMENT=""
        echo "ok"
        return 0
    fi

    local wait_start
    wait_start="${pre_start:-$(date +%s)}"
    _sig_log "Waiting for Signal reply (timeout: ${timeout}s, since: ${wait_start})..."

    while true; do
        local now
        now=$(date +%s)
        if (( now - wait_start > timeout )); then
            _sig_log "TIMEOUT: No reply after ${timeout}s"
            SIG_LAST_REPLY=""
            SIG_LAST_COMMENT=""
            echo "timeout"
            return 1
        fi

        if [[ -f "$SIG_INBOX_FILE" ]]; then
            local reply
            reply=$(python3 -c "
import json, sys
wait_start = $wait_start
with open('$SIG_INBOX_FILE') as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            msg = json.loads(line)
            ts = msg.get('ts', 0)
            # ts is in milliseconds, wait_start in seconds
            ts_sec = ts / 1000 if ts > 9999999999 else ts
            replied = msg.get('replied', False)
            body = msg.get('msg', msg.get('body', '')).strip()
            sender = msg.get('from', '')
            # Only Ophir's messages, unreplied, after wait started
            if ts_sec >= wait_start and not replied and body and sender != 'bot':
                print(body)
                sys.exit(0)
        except (json.JSONDecodeError, KeyError):
            continue
" 2>/dev/null || echo "")

            if [[ -n "$reply" ]]; then
                _sig_log "Got reply: $reply"
                SIG_LAST_REPLY="$reply"
                # Mark message as consumed so next sig_wait_reply won't pick it up
                _SIG_REPLY="$reply" python3 -c "
import json, os
wait_start = $wait_start
reply_text = os.environ['_SIG_REPLY']
inbox = '$SIG_INBOX_FILE'
lines = []
found = False
with open(inbox) as f:
    for line in f:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
            ts = msg.get('ts', 0)
            ts_sec = ts / 1000 if ts > 9999999999 else ts
            body = msg.get('msg', msg.get('body', '')).strip()
            if not found and ts_sec >= wait_start and not msg.get('replied', False) and body == reply_text:
                msg['replied'] = True
                found = True
            lines.append(json.dumps(msg))
        except (json.JSONDecodeError, KeyError):
            lines.append(stripped)
with open(inbox, 'w') as f:
    f.write('\n'.join(lines) + '\n')
" 2>/dev/null || true
                echo "$reply"
                return 0
            fi
        fi

        sleep 5
    done
}

# ── sig_checkpoint — Send message + wait for any reply ───────────────────────

sig_checkpoint() {
    local message="$1"
    local timeout="${2:-$SIG_DEFAULT_TIMEOUT}"

    # Capture wait_start BEFORE sending — if Ophir replies while sig_send
    # runs (docker exec can take seconds), we must not miss the reply.
    local pre_send_ts
    pre_send_ts=$(date +%s)
    sig_send "$message"
    sig_wait_reply "$timeout" "$pre_send_ts"
}

# ── sig_verify — Send message + wait for pass/fail verdict ───────────────────
#
# Returns: "pass", "fail", or "timeout"
# Sets SIG_LAST_COMMENT to any text after the pass/fail keyword.

sig_verify() {
    local message="$1"
    local timeout="${2:-$SIG_DEFAULT_TIMEOUT}"

    local reply
    reply=$(sig_checkpoint "$message" "$timeout") || reply="timeout"
    SIG_LAST_REPLY="$reply"

    local lower_reply
    lower_reply=$(echo "$reply" | tr '[:upper:]' '[:lower:]')
    SIG_LAST_COMMENT=""

    if [[ "$lower_reply" =~ ^(pass|passed|ok|yes|y|looks\ good|good|lgtm|all\ pass) ]]; then
        SIG_LAST_COMMENT=$(echo "$reply" | sed -E 's/^(pass(ed)?|ok|yes|y|looks good|good|lgtm|all pass)[[:space:]]*//' || true)
        echo "pass"
    elif [[ "$lower_reply" == "timeout" ]]; then
        echo "timeout"
    else
        SIG_LAST_COMMENT="$reply"
        echo "fail"
    fi
}

# ── sig_setup_step — Send a setup instruction and wait for "ok" ──────────────
#
# Aborts the calling script on timeout. Use for mandatory setup steps.

sig_setup_step() {
    local step_num="$1"
    local total_steps="$2"
    local message="$3"
    local timeout="${4:-900}"  # 15 min default for setup

    local reply
    reply=$(sig_checkpoint "🤖 Orchestrator: Setup step ${step_num}/${total_steps}

${message}

Reply 'ok' when done." "$timeout") || reply="timeout"

    local lower_reply
    lower_reply=$(echo "$reply" | tr '[:upper:]' '[:lower:]')

    if [[ "$lower_reply" == "timeout" ]]; then
        sig_send "🤖 Orchestrator: Setup step ${step_num} timed out. Aborting test."
        _sig_log "ABORT: Setup step $step_num timed out"
        return 1
    fi

    if [[ "$lower_reply" =~ ^(abort|cancel|stop|quit|nevermind) ]]; then
        sig_send "🤖 Orchestrator: Test cancelled by Ophir at step ${step_num}."
        _sig_log "ABORT: Ophir cancelled at step $step_num"
        return 1
    fi

    _sig_log "Setup step $step_num confirmed"
    return 0
}

# ── sig_action_verify — Perform an action, then ask for pass/fail ────────────
#
# Usage: sig_action_verify <step_num> <total> <action_description> <verification_question>
# Returns: "pass" or "fail"

sig_action_verify() {
    local step_num="$1"
    local total_steps="$2"
    local action_desc="$3"
    local verify_question="$4"
    local timeout="${5:-600}"

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Step ${step_num}/${total_steps} — ${action_desc}

${verify_question}

Reply 'pass' or 'fail' with optional comment." "$timeout")

    if [[ "$verdict" == "timeout" ]]; then
        sig_send "🤖 Orchestrator: Step ${step_num} verification timed out. Treating as inconclusive."
        _sig_log "Step $step_num: timeout (inconclusive)"
    elif [[ "$verdict" == "fail" ]]; then
        _sig_log "Step $step_num: FAIL${SIG_LAST_COMMENT:+ ($SIG_LAST_COMMENT)}"
    else
        _sig_log "Step $step_num: PASS${SIG_LAST_COMMENT:+ ($SIG_LAST_COMMENT)}"
    fi

    echo "$verdict"
}
