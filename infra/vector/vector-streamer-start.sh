#!/bin/sh
#
# vector-streamer-start.sh — Start vector-streamer with proper service ordering.
#
# This script manages the startup sequence required for the engine-anim
# proxy and mic socket proxy. The ordering is critical because:
#
#   1. All services must be stopped first to prevent dependency chains
#      (vic-cloud Wants=vic-engine, vic-engine Wants=vic-anim)
#   2. vic-anim must start first (creates server sockets)
#   3. vector-streamer must start next (renames sockets, sends ANKICONN
#      before any other client connects — DGRAM peer filtering)
#   4. vic-engine must start (connects through engine proxy)
#   5. vic-cloud must start last (creates mic_sock_cp_mic client)
#
# Usage:
#   vector-streamer-start.sh [--stop]
#
# Environment:
#   STREAMER_OPTS  Additional options for vector-streamer (e.g., "-v")
#

STREAMER_BIN="/anki/bin/vector-streamer"
STREAMER_LOG="/tmp/streamer.log"
STREAMER_OPTS="${STREAMER_OPTS:--e -v}"

ENGINE_ANIM_SOCK="/dev/socket/_engine_anim_server_0"
MIC_SOCK="/dev/socket/mic_sock"
MIC_SOCK_CP="/dev/socket/mic_sock_cp_mic"

log() {
    echo "[streamer-mgr] $(date '+%H:%M:%S') $*" >&2
}

wait_socket() {
    local path="$1"
    local timeout="$2"
    local waited=0
    while [ ! -S "$path" ] && [ $waited -lt $timeout ]; do
        sleep 0.5
        waited=$((waited + 1))
    done
    [ -S "$path" ]
}

cleanup() {
    log "Stopping vector-streamer..."
    killall vector-streamer 2>/dev/null
    sleep 1

    # Clean up proxy sockets
    rm -f /dev/socket/_engine_anim_server_0_orig
    rm -f /dev/socket/_engine_anim_client_vs
    rm -f /dev/socket/mic_sock_orig
    rm -f /dev/socket/mic_sock_vs

    # Restart services to normal state (order matters: anim first)
    log "Restarting services..."
    systemctl stop vic-engine 2>/dev/null
    systemctl stop vic-cloud 2>/dev/null
    systemctl stop vic-anim 2>/dev/null
    sleep 1
    systemctl start vic-anim
    sleep 3
    systemctl start vic-engine
    sleep 2
    systemctl start vic-cloud
    log "Cleanup complete"
}

# Handle --stop flag
if [ "$1" = "--stop" ]; then
    cleanup
    exit 0
fi

# Check binary exists
if [ ! -x "$STREAMER_BIN" ]; then
    log "FATAL: $STREAMER_BIN not found or not executable"
    exit 1
fi

# Kill any existing instance
killall vector-streamer 2>/dev/null
sleep 1

# Clean up stale proxy sockets from previous runs
rm -f /dev/socket/_engine_anim_server_0_orig
rm -f /dev/socket/_engine_anim_client_vs
rm -f /dev/socket/mic_sock_orig
rm -f /dev/socket/mic_sock_vs

log "=== Starting vector-streamer startup sequence ==="

# Step 1: Stop ALL services to prevent dependency chains.
# vic-cloud Wants=vic-engine, vic-engine Wants=vic-anim — if we restart
# any service, systemd may pull in others via Wants= directives.
# Stopping all first gives us full control over startup order.
log "Step 1: Stopping all services..."
systemctl stop vic-engine 2>/dev/null
systemctl stop vic-cloud 2>/dev/null
systemctl stop vic-anim 2>/dev/null
sleep 2

# Clean any leftover sockets from the stopped services
rm -f "$ENGINE_ANIM_SOCK"
rm -f /dev/socket/_engine_anim_client_0
rm -f "$MIC_SOCK"
rm -f "$MIC_SOCK_CP"

# Step 2: Start vic-anim ONLY. It has no Wants= on other services,
# so systemd won't pull in anything else.
log "Step 2: Starting vic-anim..."
systemctl start vic-anim
sleep 3

# Wait for vic-anim to create its sockets
log "Waiting for vic-anim sockets..."
if ! wait_socket "$ENGINE_ANIM_SOCK" 30; then
    log "FATAL: vic-anim didn't create $ENGINE_ANIM_SOCK"
    systemctl start vic-engine
    systemctl start vic-cloud
    exit 1
fi
if ! wait_socket "$MIC_SOCK" 10; then
    log "WARNING: mic_sock not created"
fi
log "vic-anim sockets ready"

# Verify no other service snuck in
if systemctl is-active vic-engine >/dev/null 2>&1; then
    log "WARNING: vic-engine is running — stopping"
    systemctl stop vic-engine
    sleep 1
fi

# Step 3: Start vector-streamer.
# Engine proxy: renames _engine_anim_server_0, sends ANKICONN
# Mic proxy: renames mic_sock, creates proxy at mic_sock + forwarder at mic_sock_vs
log "Step 3: Starting vector-streamer..."
LD_LIBRARY_PATH=/anki/lib nohup "$STREAMER_BIN" $STREAMER_OPTS > "$STREAMER_LOG" 2>&1 &
STREAMER_PID=$!
sleep 3

# Verify it's running
if ! kill -0 $STREAMER_PID 2>/dev/null; then
    log "FATAL: vector-streamer failed to start. Log:"
    cat "$STREAMER_LOG"
    systemctl start vic-engine
    systemctl start vic-cloud
    exit 1
fi

# Check engine proxy status
if grep -q "FATAL: Failed to send ANKICONN" "$STREAMER_LOG"; then
    log "FATAL: Engine proxy failed to send ANKICONN. Cleaning up..."
    kill $STREAMER_PID 2>/dev/null
    cleanup
    exit 1
fi

if grep -q "Sent ANKICONN handshake to vic-anim" "$STREAMER_LOG"; then
    log "Engine proxy ANKICONN successful"
else
    log "WARNING: ANKICONN status unclear — waiting 2s more..."
    sleep 2
    if grep -q "Sent ANKICONN handshake to vic-anim" "$STREAMER_LOG"; then
        log "Engine proxy ANKICONN successful (delayed)"
    else
        log "WARNING: ANKICONN may have failed"
    fi
fi

# Step 4: Start vic-engine. It has Wants=vic-anim, but vic-anim
# is already running so no restart. vic-engine connects to our
# proxy at _engine_anim_server_0.
log "Step 4: Starting vic-engine..."
systemctl start vic-engine
sleep 3

# Verify vic-engine connected through proxy
if grep -q "vic-engine connected" "$STREAMER_LOG"; then
    log "vic-engine connected through proxy"
else
    log "WARNING: vic-engine may not be connected through proxy yet"
    sleep 3
    if grep -q "vic-engine connected" "$STREAMER_LOG"; then
        log "vic-engine connected through proxy (delayed)"
    fi
fi

# Step 5: Start vic-cloud. It has Wants=vic-engine, but vic-engine
# is already running. vic-cloud creates mic_sock_cp_mic and connects
# to mic_sock.
log "Step 5: Starting vic-cloud..."
systemctl start vic-cloud

# Wait for vic-cloud to create its mic socket
log "Waiting for vic-cloud mic socket..."
if wait_socket "$MIC_SOCK_CP" 30; then
    log "vic-cloud mic socket ready"
else
    log "WARNING: vic-cloud didn't create $MIC_SOCK_CP (mic proxy may not work)"
fi

log "=== vector-streamer startup complete ==="
log "PID: $STREAMER_PID"
log "Log: $STREAMER_LOG"
log "TCP port: 5555"

# Print summary from log
grep -E "Proxy|ANKICONN|connected|Injected|FATAL|ERROR|Audio chunk|CloudMic" "$STREAMER_LOG" | while read line; do
    log "  $line"
done
