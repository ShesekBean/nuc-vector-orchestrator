#!/bin/bash
# vector-physical-test.sh — Interactive Signal-based physical test runner for Vector
#
# Runs test steps through the Vector HTTP bridge (localhost:8081) and collects
# pass/fail verdicts from Ophir via Signal.
#
# Usage:
#   bash scripts/vector-physical-test.sh [--categories cat1,cat2,...] [--issue NUM]
#
# Categories (default: all):
#   health, led, head, lift, tts, camera, display, motor, stop
#
# Prerequisites:
#   - Vector bridge running (systemctl --user start vector-bridge)
#   - OpenClaw gateway running (for Signal)
#   - Vector connected and responding

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

source "$REPO_DIR/scripts/signal-interactive.sh"

# ── Config ──────────────────────────────────────────────────────────────────

BRIDGE_URL="${VECTOR_BRIDGE_URL:-http://localhost:8081}"
ISSUE_NUM="${VECTOR_TEST_ISSUE:-0}"
TEST_CATEGORIES=""
RESULT_FILE=""

# ── Arg parsing ─────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --categories) TEST_CATEGORIES="$2"; shift 2 ;;
        --issue)      ISSUE_NUM="$2"; shift 2 ;;
        *)            shift ;;
    esac
done

if [[ "$ISSUE_NUM" != "0" ]]; then
    RESULT_FILE="/tmp/physical-test-result-${ISSUE_NUM}.json"
fi

log() { echo "[vector-test] $(date +%H:%M:%S) $*"; }

# ── Bridge helpers ──────────────────────────────────────────────────────────

bridge_get() {
    local endpoint="$1"
    curl -sf --max-time 10 "${BRIDGE_URL}${endpoint}" 2>/dev/null
}

bridge_post() {
    local endpoint="$1"
    local data="${2:-{}}"
    curl -sf --max-time 10 -X POST "${BRIDGE_URL}${endpoint}" \
        -H "Content-Type: application/json" \
        -d "$data" 2>/dev/null
}

# ── Health preflight ────────────────────────────────────────────────────────

check_bridge_health() {
    log "Checking bridge health..."
    local result
    result=$(bridge_get "/health") || {
        log "ERROR: Bridge not responding at $BRIDGE_URL"
        sig_send "🤖 Orchestrator: Vector bridge is not responding at $BRIDGE_URL. Cannot run physical tests. Start the bridge first: systemctl --user start vector-bridge"
        return 1
    }

    local status
    status=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

    if [[ "$status" != "healthy" ]]; then
        log "ERROR: Bridge reports unhealthy: $result"
        sig_send "🤖 Orchestrator: Vector bridge is not healthy. Check robot connection."
        return 1
    fi

    local battery
    battery=$(echo "$result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
b = d.get('battery', {})
if isinstance(b, dict):
    level = b.get('battery_level', b.get('level', '?'))
    volts = b.get('battery_volts', b.get('voltage', '?'))
    print(f'{level}% ({volts}V)')
else:
    print(str(b))
" 2>/dev/null) || battery="unknown"

    log "Bridge healthy — battery: $battery"
    return 0
}

# ── Test categories ─────────────────────────────────────────────────────────

TOTAL_STEPS=0
PASSED_STEPS=0
FAILED_STEPS=0
FAILED_NAMES=""

record_result() {
    local name="$1"
    local verdict="$2"
    TOTAL_STEPS=$((TOTAL_STEPS + 1))
    if [[ "$verdict" == "pass" ]]; then
        PASSED_STEPS=$((PASSED_STEPS + 1))
        log "$name: PASS"
    else
        FAILED_STEPS=$((FAILED_STEPS + 1))
        FAILED_NAMES="${FAILED_NAMES:+$FAILED_NAMES, }$name"
        log "$name: FAIL"
    fi
}

# ── Health test ─────────────────────────────────────────────────────────────

test_health() {
    log "── Health check ──"
    local result
    result=$(bridge_get "/health") || { record_result "health" "fail"; return; }

    local info
    info=$(echo "$result" | python3 -c "
import json, sys
d = json.load(sys.stdin)
b = d.get('battery', {})
lat = d.get('latency_ms', '?')
if isinstance(b, dict):
    level = b.get('battery_level', b.get('level', '?'))
    print(f'Battery: {level}%, Latency: {lat}ms')
else:
    print(f'Battery: {b}, Latency: {lat}ms')
" 2>/dev/null) || info="raw: $result"

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Vector health check

$info

Does the robot appear powered on and connected?

Reply 'pass' or 'fail'.")
    record_result "health" "$verdict"
}

# ── LED tests ───────────────────────────────────────────────────────────────

test_led() {
    log "── LED tests ──"

    # Test named states
    for state in person_detected idle searching; do
        bridge_post "/led" "{\"state\": \"$state\"}" > /dev/null || true
        sleep 1

        local verdict
        verdict=$(sig_verify "🤖 Orchestrator: LED test — state '$state'

Vector's backpack LEDs should be showing the '$state' pattern.

Do the LEDs show a visible color pattern?

Reply 'pass' or 'fail'.")
        record_result "led-$state" "$verdict"
    done

    # Test hue override
    bridge_post "/led" '{"hue": 0.66, "saturation": 1.0, "duration_s": 10}' > /dev/null || true
    sleep 1

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: LED test — blue hue override

Vector's backpack LEDs should be showing solid blue (hue override).

Do the LEDs show blue?

Reply 'pass' or 'fail'.")
    record_result "led-hue-override" "$verdict"

    # Reset LEDs to idle
    bridge_post "/led" '{"state": "idle"}' > /dev/null || true
}

# ── Head tests ──────────────────────────────────────────────────────────────

test_head() {
    log "── Head movement tests ──"

    # Move head down (minimum angle)
    bridge_post "/head" '{"angle_deg": -22}' > /dev/null || true
    sleep 2

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Head test — look down

Vector's head should be tilted all the way DOWN (looking at the ground).

Is the head pointing down?

Reply 'pass' or 'fail'.")
    record_result "head-down" "$verdict"

    # Move head up (maximum angle)
    bridge_post "/head" '{"angle_deg": 45}' > /dev/null || true
    sleep 2

    verdict=$(sig_verify "🤖 Orchestrator: Head test — look up

Vector's head should now be tilted all the way UP (looking at ceiling).

Is the head pointing up?

Reply 'pass' or 'fail'.")
    record_result "head-up" "$verdict"

    # Return to center
    bridge_post "/head" '{"angle_deg": 10}' > /dev/null || true
}

# ── Lift tests ──────────────────────────────────────────────────────────────

test_lift() {
    log "── Lift tests ──"

    # Move lift to high position
    bridge_post "/lift" '{"preset": "high"}' > /dev/null || true
    sleep 2

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Lift test — high position

Vector's forklift arm should be raised to its highest position.

Is the lift arm raised up?

Reply 'pass' or 'fail'.")
    record_result "lift-high" "$verdict"

    # Move lift to low position
    bridge_post "/lift" '{"preset": "low"}' > /dev/null || true
    sleep 2

    verdict=$(sig_verify "🤖 Orchestrator: Lift test — low position

Vector's forklift arm should now be lowered to its lowest position.

Is the lift arm lowered?

Reply 'pass' or 'fail'.")
    record_result "lift-low" "$verdict"
}

# ── TTS/audio tests ────────────────────────────────────────────────────────

test_tts() {
    log "── TTS tests ──"

    sig_send "🤖 Orchestrator: TTS test — Vector will speak. Listen carefully."
    sleep 1

    bridge_post "/audio/play" '{"text": "Hello Ophir, this is Vector speaking. Testing one two three."}' > /dev/null || true
    sleep 3

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: TTS test

Did you hear Vector say 'Hello Ophir, this is Vector speaking. Testing one two three'?

Reply 'pass' or 'fail'.")
    record_result "tts" "$verdict"
}

# ── Camera tests ────────────────────────────────────────────────────────────

test_camera() {
    log "── Camera tests ──"

    local result
    result=$(bridge_get "/capture?format=base64") || {
        record_result "camera" "fail"
        sig_send "🤖 Orchestrator: Camera capture failed — bridge returned error."
        return
    }

    local size
    size=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('size_bytes', 0))" 2>/dev/null) || size=0

    if [[ "$size" -gt 0 ]]; then
        local verdict
        verdict=$(sig_verify "🤖 Orchestrator: Camera test

Successfully captured a frame from Vector's camera ($size bytes).

Can you confirm Vector's camera lens is not obstructed? (The image was captured successfully via API.)

Reply 'pass' or 'fail'.")
        record_result "camera" "$verdict"
    else
        record_result "camera" "fail"
        sig_send "🤖 Orchestrator: Camera capture returned empty image."
    fi
}

# ── Display tests ───────────────────────────────────────────────────────────

test_display() {
    log "── Display tests ──"

    bridge_post "/display" '{"expression": "happy"}' > /dev/null || true
    sleep 2

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Display test — happy expression

Vector's face screen should be showing a 'happy' expression.

Does Vector's face look happy/content?

Reply 'pass' or 'fail'.")
    record_result "display-happy" "$verdict"

    bridge_post "/display" '{"expression": "sad"}' > /dev/null || true
    sleep 2

    verdict=$(sig_verify "🤖 Orchestrator: Display test — sad expression

Vector's face screen should now be showing a 'sad' expression.

Does Vector's face look different from the previous one (sad/unhappy)?

Reply 'pass' or 'fail'.")
    record_result "display-sad" "$verdict"
}

# ── Motor tests (physical-only — requires Ophir watching) ──────────────────

test_motor() {
    log "── Motor tests ──"

    sig_send "🤖 Orchestrator: Motor test starting. Stand clear of Vector — it will move."
    sleep 2

    # Turn in place
    bridge_post "/move" '{"type": "turn", "angle_deg": 90, "speed_dps": 100}' > /dev/null || true
    sleep 3

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Motor test — turn right 90°

Vector should have turned approximately 90° to its right.

Did Vector turn roughly 90° clockwise?

Reply 'pass' or 'fail'.")
    record_result "motor-turn" "$verdict"

    # Drive straight
    bridge_post "/move" '{"type": "straight", "distance_mm": 100, "speed_mmps": 80}' > /dev/null || true
    sleep 3

    verdict=$(sig_verify "🤖 Orchestrator: Motor test — drive forward 10cm

Vector should have driven forward approximately 10 centimeters.

Did Vector move forward a short distance (~10cm)?

Reply 'pass' or 'fail'.")
    record_result "motor-forward" "$verdict"

    # Return — drive back and turn back
    bridge_post "/move" '{"type": "straight", "distance_mm": -100, "speed_mmps": 80}' > /dev/null || true
    sleep 2
    bridge_post "/move" '{"type": "turn", "angle_deg": -90, "speed_dps": 100}' > /dev/null || true
    sleep 2
}

# ── Emergency stop test ─────────────────────────────────────────────────────

test_stop() {
    log "── Emergency stop test ──"

    # Start slow wheels movement
    bridge_post "/move" '{"type": "wheels", "left_speed": 50, "right_speed": 50}' > /dev/null || true
    sleep 1

    # Emergency stop
    local result
    result=$(bridge_post "/stop") || result=""
    sleep 1

    local verdict
    verdict=$(sig_verify "🤖 Orchestrator: Emergency stop test

Vector was moving forward slowly, then emergency stop was sent.

Did Vector stop immediately?

Reply 'pass' or 'fail'.")
    record_result "emergency-stop" "$verdict"
}

# ── Main ────────────────────────────────────────────────────────────────────

main() {
    log "Starting Vector physical test suite"
    log "Bridge: $BRIDGE_URL, Issue: $ISSUE_NUM"

    # Preflight
    if ! check_bridge_health; then
        log "Preflight failed — aborting"
        exit 1
    fi

    sig_send "🤖 Orchestrator: Vector physical test starting. I'll send each test step one at a time and ask for your verdict."

    # Determine which categories to run
    local categories
    if [[ -n "$TEST_CATEGORIES" ]]; then
        IFS=',' read -ra categories <<< "$TEST_CATEGORIES"
    else
        categories=(health led head lift tts camera display motor stop)
    fi

    for cat in "${categories[@]}"; do
        case "$cat" in
            health)  test_health ;;
            led)     test_led ;;
            head)    test_head ;;
            lift)    test_lift ;;
            tts)     test_tts ;;
            camera)  test_camera ;;
            display) test_display ;;
            motor)   test_motor ;;
            stop)    test_stop ;;
            *)       log "Unknown category: $cat — skipping" ;;
        esac
    done

    # ── Summary ─────────────────────────────────────────────────────────────

    local overall
    if [[ "$FAILED_STEPS" -eq 0 ]]; then
        overall="PASS"
    else
        overall="FAIL"
    fi

    log "Test complete: $PASSED_STEPS/$TOTAL_STEPS passed ($FAILED_STEPS failed)"

    local summary="🤖 Orchestrator: Vector physical test complete!

Result: $overall — $PASSED_STEPS/$TOTAL_STEPS passed"

    if [[ -n "$FAILED_NAMES" ]]; then
        summary="$summary
Failed: $FAILED_NAMES"
    fi

    sig_send "$summary"

    # Write result file
    if [[ -n "$RESULT_FILE" ]]; then
        python3 -c "
import json
json.dump({
    'verdict': '$(echo "$overall" | tr '[:upper:]' '[:lower:]')',
    'total': $TOTAL_STEPS,
    'passed': $PASSED_STEPS,
    'failed': $FAILED_STEPS,
    'failed_names': '${FAILED_NAMES}',
    'comment': '$PASSED_STEPS/$TOTAL_STEPS passed'
}, open('$RESULT_FILE', 'w'), indent=2)
"
        log "Result written to $RESULT_FILE"
    fi

    # Post on GitHub if issue specified
    if [[ "$ISSUE_NUM" != "0" ]]; then
        gh issue comment "$ISSUE_NUM" -R ophir-sw/nuc-vector-orchestrator -b "## Orchestrator: Vector Physical Test Result

**Verdict:** $overall
**Steps:** $PASSED_STEPS/$TOTAL_STEPS passed | $FAILED_STEPS failed
${FAILED_NAMES:+**Failed:** $FAILED_NAMES}

Categories tested: $(IFS=,; echo "${categories[*]}")" 2>/dev/null || log "WARN: failed to post result comment"
    fi

    log "Done."
    [[ "$overall" == "PASS" ]] && exit 0 || exit 1
}

main
