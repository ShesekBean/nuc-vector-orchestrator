#!/bin/bash
# run-physical-test.sh — Generic interactive physical test runner
#
# Supports two modes (auto-detected from test spec):
#
# 1. LIVEKIT AUTOMATED — for comprehensive tests
#    Detected when: title contains "comprehensive" or setup mentions "LiveKit"
#    Flow: send LiveKit URL → auto-poll until user joins → verify camera → run automated_test.py → post results
#
# 2. MANUAL — for single-feature tests
#    Flow: send instructions → wait for pass/fail from Ophir → post result
#
# Usage: bash scripts/run-physical-test.sh <test-spec-json>
#
# The test-spec JSON must contain:
#   issue_num, repo, setup_command, observe, pass_criteria, fail_criteria
#
# Optional fields:
#   setup_steps[]  — array of {description, command, verify_question} for multi-step setup
#   title          — issue title (used for mode detection)
#
# Exit codes: 0 = pass, 1 = fail, 2 = timeout, 3 = abort

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

source "$REPO_DIR/scripts/signal-interactive.sh"

SPEC_JSON="${1:?Usage: run-physical-test.sh <test-spec-json>}"

# Parse test spec
_jq() { echo "$SPEC_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$1','$2'))"; }

PT_ISSUE=$(_jq issue_num "")
PT_REPO=$(_jq repo "")
PT_SETUP=$(_jq setup_command "")
PT_OBSERVE=$(_jq observe "")
PT_PASS=$(_jq pass_criteria "")
PT_FAIL=$(_jq fail_criteria "")
PT_TITLE=$(_jq title "Physical Test")
PT_SETUP_STEPS=$(echo "$SPEC_JSON" | python3 -c "
import json,sys
d = json.load(sys.stdin)
steps = d.get('setup_steps', [])
print(json.dumps(steps))
" 2>/dev/null || echo "[]")

log() { echo "[physical-test] $(date +%H:%M:%S) $*"; }

SETUP_COUNT=$(echo "$PT_SETUP_STEPS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
RESULT_FILE="/tmp/physical-test-result-${PT_ISSUE}.json"
STATE_DIR=".claude/state"

cleanup() {
    # TODO: Adapt for Vector gRPC cleanup if needed
    :
}
trap cleanup EXIT

# ── Mode detection ───────────────────────────────────────────────────────────

MODE="manual"
LOWER_TITLE=$(echo "$PT_TITLE" | tr '[:upper:]' '[:lower:]')
LOWER_SETUP=$(echo "$PT_SETUP" | tr '[:upper:]' '[:lower:]')

if [[ "$LOWER_TITLE" == *"comprehensive"* ]] || [[ "$LOWER_SETUP" == *"livekit"* ]]; then
    MODE="livekit"
fi

log "Test #${PT_ISSUE} — mode: $MODE — $PT_TITLE"

# ══════════════════════════════════════════════════════════════════════════════
# LIVEKIT AUTOMATED MODE
# ══════════════════════════════════════════════════════════════════════════════

if [[ "$MODE" == "livekit" ]]; then

    # ── Auto-poll LiveKit — send URL only if user isn't already in room ──────
    log "Auto-polling LiveKit every 30s (5 min timeout)..."
    POLL_INTERVAL=30
    MAX_POLL_TIME=300
    POLL_START=$(date +%s)
    URL_SENT=false
    VERIFY_OK=false

    while true; do
        ELAPSED=$(( $(date +%s) - POLL_START ))
        if [[ "$ELAPSED" -ge "$MAX_POLL_TIME" ]]; then
            sig_send "🤖 Orchestrator: Timed out after 5 minutes waiting for valid camera view. Test aborted."
            exit 2
        fi

        log "Polling LiveKit... (${ELAPSED}s elapsed)"

        # Try to capture a frame
        FRAME_PATH=$(python3 -c "
from apps.test_harness.camera_capture import CameraCapture
cam = CameraCapture()
p = cam.capture_and_save('livekit_verify')
print(p)
" 2>/dev/null)
        CAPTURE_EXIT=$?

        if [[ "$CAPTURE_EXIT" -ne 0 ]]; then
            log "No one in LiveKit room yet."
            if [[ "$URL_SENT" == "false" ]]; then
                # Generate or reuse cached URL
                CACHE_FILE=".claude/state/livekit-join-url.json"
                JOIN_URL=""
                if [[ -f "$CACHE_FILE" ]]; then
                    JOIN_URL=$(python3 -c "
import json, time, jwt
data = json.load(open('$CACHE_FILE'))
url = data.get('url', '')
token = url.split('token=')[-1] if 'token=' in url else ''
if token:
    payload = jwt.decode(token, options={'verify_signature': False})
    if time.time() < payload.get('exp', 0) - 300:
        print(url)
" 2>/dev/null) || JOIN_URL=""
                fi
                if [[ -z "$JOIN_URL" ]]; then
                    JOIN_URL=$(python3 -c "
from apps.test_harness.camera_capture import CameraCapture
cam = CameraCapture()
print(cam.generate_join_url())
" 2>/dev/null) || {
                        sig_send "🤖 Orchestrator: Failed to generate LiveKit URL. Check .env.livekit credentials."
                        exit 1
                    }
                    mkdir -p "$(dirname "$CACHE_FILE")"
                    python3 -c "import json, time; json.dump({'url': '$JOIN_URL', 'created': time.time()}, open('$CACHE_FILE', 'w'))" 2>/dev/null || true
                fi
                sig_send "🤖 Orchestrator: Comprehensive test #${PT_ISSUE} ready.

Tap to join LiveKit:

${JOIN_URL}

Point camera at robot (~0.5m), speaker facing robot mic. I'll detect when you're in the room and start automatically."
                URL_SENT=true
            fi
            sleep "$POLL_INTERVAL"
            continue
        fi

        # User is in the room — verify camera sees the robot
        log "User detected in LiveKit room! Validating camera view..."
        ROBOT_CHECK=$(python3 -c "
from apps.test_harness.action_evaluator import evaluate_action
result = evaluate_action('$FRAME_PATH', '$FRAME_PATH',
    'Is there a robot visible in this image? Look for a wheeled robot platform, robot chassis, or robotic vehicle. Answer based on the FIRST image only.')
print('yes' if result.get('success', False) else 'no')
print(result.get('explanation', 'unknown'))
" 2>/dev/null) || ROBOT_CHECK="no
vision check failed"

        ROBOT_VISIBLE=$(echo "$ROBOT_CHECK" | head -1)
        EXPLANATION=$(echo "$ROBOT_CHECK" | tail -1)

        if [[ "$ROBOT_VISIBLE" == "yes" ]]; then
            VERIFY_OK=true
            break
        fi

        sig_send "🤖 Orchestrator: I can see you're in the room but the camera view isn't right. ($EXPLANATION)

Please adjust so I can see the robot. I'll check again in 30s."
        sleep "$POLL_INTERVAL"
    done

    if [[ "$VERIFY_OK" != "true" ]]; then
        sig_send "🤖 Orchestrator: Still can't see the robot. Proceeding anyway — vision checks may be inaccurate."
    fi

    sig_send "🤖 Orchestrator: Camera verified. Running automated test now — this will take several minutes. No action needed from you."

    # ── Run automated test ───────────────────────────────────────────────────
    log "Running automated_test.py..."
    TEST_OUTPUT=$(python3 apps/test_harness/automated_test.py --target-class 62 2>&1) || true
    TEST_EXIT=$?
    log "automated_test.py exited with code $TEST_EXIT"

    # ── Parse results ────────────────────────────────────────────────────────
    REPORT_FILE="apps/test_harness/captures/autotest/report.json"
    if [[ -f "$REPORT_FILE" ]]; then
        TOTAL=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print(r['total'])")
        PASSED=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print(r['passed'])")
        DURATION=$(python3 -c "import json; r=json.load(open('$REPORT_FILE')); print(f\"{r['duration_s']:.0f}\")")
        FAILED=$((TOTAL - PASSED))

        DETAILS=$(python3 -c "
import json
r = json.load(open('$REPORT_FILE'))
for t in r['results']:
    status = 'PASS' if t['passed'] else 'FAIL'
    conf = f\" ({t['confidence']:.0%})\" if t['method'] == 'vision' else ''
    print(f\"| {t['phase']} | {t['name']} | {status}{conf} | {t['details'][:80]} |\")
")
    else
        TOTAL=0
        PASSED=0
        FAILED=1
        DURATION="?"
        DETAILS="| ? | automated_test.py crashed | FAIL | See output below |"
    fi

    # ── Notify Ophir ─────────────────────────────────────────────────────────
    if [[ "$FAILED" -eq 0 && "$TOTAL" -gt 0 ]]; then
        VERDICT="PASS"
        sig_send "🤖 Orchestrator: Comprehensive test #${PT_ISSUE} PASSED!

${PASSED}/${TOTAL} phases passed in ${DURATION}s."
    else
        VERDICT="FAIL"
        FAILED_NAMES=$(python3 -c "
import json, os
r = json.load(open('$REPORT_FILE')) if os.path.exists('$REPORT_FILE') else {'results': []}
failed = [f\"{t['phase']}: {t['name']}\" for t in r['results'] if not t['passed']]
print(', '.join(failed) if failed else 'unknown')
" 2>/dev/null || echo "unknown")

        sig_send "🤖 Orchestrator: Comprehensive test #${PT_ISSUE} — ${PASSED}/${TOTAL} passed, ${FAILED} failed.

Failed: ${FAILED_NAMES}

Worker will investigate."
    fi

    # ── Post on GitHub ───────────────────────────────────────────────────────
    SHORT_OUTPUT=$(echo "$TEST_OUTPUT" | tail -40)

    gh issue comment "$PT_ISSUE" -R "$PT_REPO" -b "## Orchestrator: Automated Comprehensive Test Result

**Verdict:** ${VERDICT}
**Phases:** ${PASSED}/${TOTAL} passed | ${FAILED} failed | ${DURATION}s

| Phase | Test | Result | Details |
|-------|------|--------|---------|
${DETAILS}

<details>
<summary>Test output (last 40 lines)</summary>

\`\`\`
${SHORT_OUTPUT}
\`\`\`
</details>
" 2>/dev/null || log "WARN: failed to post result comment"

    echo "{\"verdict\": \"$(echo "$VERDICT" | tr '[:upper:]' '[:lower:]')\", \"comment\": \"${PASSED}/${TOTAL} passed\"}" > "$RESULT_FILE"

# ══════════════════════════════════════════════════════════════════════════════
# MANUAL MODE
# ══════════════════════════════════════════════════════════════════════════════

else
    # ── Quick ack + silent setup ─────────────────────────────────────────────
    sig_send "🤖 Orchestrator: Setting up test #${PT_ISSUE} — ${PT_TITLE}. Will let you know when ready."

    # ── Setup steps ──────────────────────────────────────────────────────────
    STEP_NUM=0

    if [[ "$SETUP_COUNT" -gt 0 ]]; then
        if echo "$PT_SETUP_STEPS" | python3 -c "import json,sys; sys.exit(0 if len(json.load(sys.stdin)) > 0 else 1)" 2>/dev/null; then
            echo "$PT_SETUP_STEPS" | python3 -c "
import json, sys
steps = json.load(sys.stdin)
for i, step in enumerate(steps):
    print(json.dumps(step))
" 2>/dev/null | while IFS= read -r step_json; do
                STEP_NUM=$((STEP_NUM + 1))
                step_desc=$(echo "$step_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('description','Run setup'))")
                step_cmd=$(echo "$step_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('command',''))")
                step_verify=$(echo "$step_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('verify_question','Did this step complete successfully?'))")

                if [[ -n "$step_cmd" ]]; then
                    sig_send "🤖 Orchestrator: Step ${STEP_NUM}/${SETUP_COUNT} — ${step_desc}

Running command now..."
                    cmd_result=""
                    # TODO: Add Vector gRPC command dispatch for component:vector issues
                    cmd_result=$(eval "$step_cmd" 2>&1 | tail -20) || cmd_result="Command failed: $cmd_result"
                    log "Step $STEP_NUM command result: $cmd_result"
                fi

                step_verdict=$(sig_verify "🤖 Orchestrator: Step ${STEP_NUM}/${SETUP_COUNT} — ${step_desc}

${step_verify}

Reply 'pass' or 'fail' with optional comment." 600)

                if [[ "$step_verdict" == "fail" ]]; then
                    sig_send "🤖 Orchestrator: Step ${STEP_NUM} failed${SIG_LAST_COMMENT:+: $SIG_LAST_COMMENT}. Aborting test."
                    echo "{\"verdict\": \"fail\", \"step\": $STEP_NUM, \"comment\": \"$(echo "${SIG_LAST_COMMENT:-Step $STEP_NUM failed}" | sed 's/"/\\"/g')\"}" > "$RESULT_FILE"
                    exit 1
                elif [[ "$step_verdict" == "timeout" ]]; then
                    sig_send "🤖 Orchestrator: Step ${STEP_NUM} timed out. Aborting test."
                    exit 2
                fi
            done
        else
            STEP_NUM=1
            sig_send "🤖 Orchestrator: Setting up robot... (automated, no action needed from you)"

            setup_exit=0
            setup_result=""
            # TODO: Add Vector gRPC command dispatch for component:vector issues
            if [[ -n "$PT_SETUP" ]]; then
                log "Running local setup: $PT_SETUP"
                setup_result=$(eval "$PT_SETUP" 2>&1 | tail -20) || setup_exit=$?
            fi
            log "Setup result (exit=$setup_exit): $setup_result"

            if [[ "$setup_exit" -ne 0 ]]; then
                sig_send "🤖 Orchestrator: Setup failed (exit code $setup_exit). Aborting test.

${setup_result}"
                echo "{\"verdict\": \"fail\", \"step\": 1, \"comment\": \"Setup failed (exit $setup_exit)\"}" > "$RESULT_FILE"
                exit 1
            fi
            log "Setup succeeded — proceeding automatically"
        fi
    fi

    # ── Test — observe and give verdict ──────────────────────────────────────
    _test_msg="🤖 Orchestrator: Robot is ready. Here's what to test:

${PT_OBSERVE}"

    if [[ -n "$PT_PASS" ]]; then
        _test_msg="${_test_msg}

Pass: ${PT_PASS}"
    fi
    if [[ -n "$PT_FAIL" ]]; then
        _test_msg="${_test_msg}
Fail: ${PT_FAIL}"
    fi

    _test_msg="${_test_msg}

Reply 'pass' or 'fail' when done."

    final_verdict=$(sig_verify "$_test_msg" 900)
    FINAL_COMMENT="${SIG_LAST_COMMENT:-}"
    VERDICT=$(echo "$final_verdict" | tr '[:lower:]' '[:upper:]')

    if [[ "$final_verdict" == "pass" ]]; then
        sig_send "🤖 Orchestrator: Test PASSED!${FINAL_COMMENT:+ ($FINAL_COMMENT)} Cleaning up."
        echo "{\"verdict\": \"pass\", \"comment\": \"$(echo "${FINAL_COMMENT:-}" | sed 's/"/\\"/g')\"}" > "$RESULT_FILE"
    elif [[ "$final_verdict" == "timeout" ]]; then
        sig_send "🤖 Orchestrator: Verdict timed out. Stopping robot."
        echo "{\"verdict\": \"timeout\", \"comment\": \"Ophir did not respond to verdict\"}" > "$RESULT_FILE"
    else
        sig_send "🤖 Orchestrator: Test FAILED.${FINAL_COMMENT:+ ($FINAL_COMMENT)} Worker will investigate."
        echo "{\"verdict\": \"fail\", \"comment\": \"$(echo "${FINAL_COMMENT:-}" | sed 's/"/\\"/g')\"}" > "$RESULT_FILE"
    fi

    # Post result on GitHub
    gh issue comment "$PT_ISSUE" -R "$PT_REPO" -b "## Orchestrator: Physical Test Result (Interactive)

**Verdict:** ${VERDICT}
**Feedback:** ${FINAL_COMMENT:-No comment}
**Test:** #${PT_ISSUE}

Worker will be re-dispatched to evaluate." 2>/dev/null || log "WARN: failed to post result comment"

fi

# ── Common cleanup ───────────────────────────────────────────────────────────

# Remove blocker and reset PGM gate
gh issue edit "$PT_ISSUE" -R "$PT_REPO" --remove-label "blocker:needs-human" --remove-label "stuck" 2>/dev/null || true
sed -i "/\-${PT_ISSUE}	/d" "$STATE_DIR/pgm-signal-sent.tsv" 2>/dev/null || true

# Clean up physical test state file
rm -f "$STATE_DIR/physical-test-pending.json"

log "Physical test complete: ${VERDICT:-unknown}"
final_lc=$(echo "${VERDICT:-FAIL}" | tr '[:upper:]' '[:lower:]')
exit "$([[ "$final_lc" == "pass" ]] && echo 0 || echo 1)"
