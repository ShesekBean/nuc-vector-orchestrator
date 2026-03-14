#!/bin/sh
#
# vector-streamer-start.sh — Start/stop the vector-streamer socket proxy.
#
# This script does NOT manage other services. Systemd handles ordering via
# Before=vic-engine.service in vector-streamer.service. The boot sequence:
#
#   vic-anim starts (creates server sockets)
#   → this script runs (waits for sockets, launches proxy, verifies ready)
#   → script exits (Type=forking → systemd knows we're ready)
#   → systemd starts vic-engine (connects through our proxy)
#   → systemd starts vic-cloud (connects through our mic proxy)
#
# Usage:
#   vector-streamer-start.sh          # Start proxy
#   vector-streamer-start.sh --stop   # Stop proxy, clean up sockets
#
# Environment:
#   STREAMER_OPTS  Additional options for vector-streamer (default: "-e -v")

STREAMER_BIN="/anki/bin/vector-streamer"
STREAMER_LOG="/tmp/streamer.log"
STREAMER_PID_FILE="/tmp/vector-streamer.pid"
STREAMER_OPTS="${STREAMER_OPTS:--e -v}"

ENGINE_ANIM_SOCK="/dev/socket/_engine_anim_server_0"
MIC_SOCK="/dev/socket/mic_sock"

PROXY_SOCKETS="
/dev/socket/_engine_anim_server_0_orig
/dev/socket/_engine_anim_client_vs
/dev/socket/_engine_anim_client_0
/dev/socket/mic_sock_orig
/dev/socket/mic_sock_vs
"

log() {
    echo "[streamer-mgr] $(date '+%H:%M:%S') $*" >&2
}

wait_socket() {
    local path="$1"
    local timeout="$2"
    local waited=0
    while [ ! -S "$path" ] && [ $waited -lt $timeout ]; do
        sleep 1
        waited=$((waited + 1))
    done
    [ -S "$path" ]
}

# ── Stop ────────────────────────────────────────────────────────
do_stop() {
    log "Stopping vector-streamer..."
    killall vector-streamer 2>/dev/null
    sleep 1
    rm -f "$STREAMER_PID_FILE"

    # Remove proxy sockets so services reconnect directly on next boot
    for sock in $PROXY_SOCKETS; do
        rm -f "$sock"
    done

    log "Stopped"
}

if [ "$1" = "--stop" ]; then
    do_stop
    exit 0
fi

# ── Start ───────────────────────────────────────────────────────

# Preflight
if [ ! -x "$STREAMER_BIN" ]; then
    log "FATAL: $STREAMER_BIN not found or not executable"
    exit 1
fi

# Kill any stale instance
killall vector-streamer 2>/dev/null
sleep 1
rm -f "$STREAMER_PID_FILE"

# Clean stale proxy sockets from a previous run
for sock in $PROXY_SOCKETS; do
    rm -f "$sock"
done

log "=== vector-streamer startup ==="

# ── Wait for vic-anim sockets ──────────────────────────────────
# vic-anim creates these on startup. Systemd's After=vic-anim.service
# ensures vic-anim's unit is started, but the sockets may take a moment.

log "Waiting for vic-anim sockets..."
if ! wait_socket "$ENGINE_ANIM_SOCK" 60; then
    log "FATAL: $ENGINE_ANIM_SOCK not created after 60s"
    exit 1
fi
if ! wait_socket "$MIC_SOCK" 30; then
    log "WARNING: $MIC_SOCK not created (mic proxy won't work)"
fi
log "vic-anim sockets ready"

# ── Launch vector-streamer ──────────────────────────────────────
# The binary renames the original sockets and creates proxy sockets
# in their place. vic-engine/vic-cloud will connect to the proxies.

log "Launching vector-streamer..."
LD_LIBRARY_PATH=/anki/lib nohup "$STREAMER_BIN" $STREAMER_OPTS > "$STREAMER_LOG" 2>&1 &
STREAMER_PID=$!
echo $STREAMER_PID > "$STREAMER_PID_FILE"

# ── Verify proxy is ready ──────────────────────────────────────
# Wait for the binary to set up proxy sockets.
# ANKICONN is deferred — it happens when vic-engine starts (after this
# script exits and systemd proceeds via Before= ordering).
# We just need to see "vector-streamer ready" in the log.

log "Verifying proxy setup..."
ready=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    # Check process is alive
    if ! kill -0 "$STREAMER_PID" 2>/dev/null; then
        log "FATAL: vector-streamer died during startup"
        cat "$STREAMER_LOG" >&2
        rm -f "$STREAMER_PID_FILE"
        exit 1
    fi

    # Check for fatal errors
    if grep -q "FATAL" "$STREAMER_LOG" 2>/dev/null; then
        log "FATAL: error in streamer log"
        cat "$STREAMER_LOG" >&2
        kill "$STREAMER_PID" 2>/dev/null
        rm -f "$STREAMER_PID_FILE"
        exit 1
    fi

    # Check for ready signal
    if grep -q "vector-streamer ready" "$STREAMER_LOG" 2>/dev/null; then
        ready=1
        break
    fi

    sleep 1
done

if [ $ready -eq 0 ]; then
    log "WARNING: ready signal not seen after 10s (continuing anyway)"
fi

# Check engine proxy status (ANKICONN is deferred until vic-engine starts)
if grep -q "ANKICONN deferred" "$STREAMER_LOG" 2>/dev/null; then
    log "Engine proxy ready (ANKICONN deferred until vic-engine starts)"
elif grep -q "Engine proxy:   disabled" "$STREAMER_LOG" 2>/dev/null; then
    log "Engine proxy disabled"
fi

log "=== vector-streamer ready (PID $STREAMER_PID, port 5555) ==="

# Exit — Type=forking means systemd now considers us "active"
# and proceeds to start vic-engine, vic-cloud via Before= ordering.
