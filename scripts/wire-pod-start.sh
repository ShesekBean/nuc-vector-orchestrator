#!/bin/bash
# Wire-pod launcher for systemd
cd /home/ophirsw/Documents/claude/wire-pod/chipper || exit 1

export HOME=/root
export GOPATH=/root/go
export GOMODCACHE=/root/go/pkg/mod
export DEBUG_LOGGING=true
export STT_SERVICE=vosk
export CGO_ENABLED=1
export CGO_CFLAGS="-I/root/.vosk/libvosk"
export CGO_LDFLAGS="-L/root/.vosk/libvosk -lvosk -ldl -lpthread"
export LD_LIBRARY_PATH="/root/.vosk/libvosk"

COMMIT_HASH="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

exec /usr/local/go/bin/go run \
    -tags nolibopusfile \
    -ldflags="-X 'github.com/kercre123/wire-pod/chipper/pkg/vars.CommitSHA=${COMMIT_HASH}'" \
    -exec "env DYLD_LIBRARY_PATH=/root/.vosk/libvosk" \
    cmd/vosk/main.go
