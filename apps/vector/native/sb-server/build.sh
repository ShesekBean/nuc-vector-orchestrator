#!/bin/bash
# Build Porcupine v4 wake word components for Vector (WireOS/OSKR)
# Run on Jetson (192.168.1.70) where both toolchains are available
#
# Two-process architecture:
#   1. pv_shim (soft-float) — replaces libpv_porcupine_softfp.so, loaded by vic-anim
#   2. pv_worker (hard-float) — standalone daemon, runs Porcupine v4 via dlopen
#
# They communicate via Unix domain socket at /dev/_pv_worker_.
#
# Prerequisites:
#   - vicos-sdk 5.3.0-r07 at ~/.anki/vicos-sdk/dist/5.3.0-r07/
#   - arm-linux-gnueabihf-gcc (apt install gcc-arm-linux-gnueabihf)
#   - RPi ARM11 libpv_porcupine.so in /anki/lib/hf/ on Vector (hard-float runtime)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Soft-float toolchain (vicos SDK) ---
VICOS_SDK="${HOME}/.anki/vicos-sdk/dist/5.3.0-r07"
VICOS_CLANG="${VICOS_SDK}/prebuilt/bin/clang"
SYSROOT="${VICOS_SDK}/sysroot"

# Try alternative paths if default not found
if [ ! -x "$VICOS_CLANG" ]; then
    for candidate in \
        /tmp/victor-build/victor/vicos-sdk/dist/*/prebuilt/bin/clang \
        /opt/vicos-sdk/*/prebuilt/bin/clang; do
        if [ -x "$candidate" ]; then
            VICOS_CLANG="$candidate"
            SYSROOT="$(dirname "$(dirname "$candidate")")/sysroot"
            break
        fi
    done
fi

if [ ! -x "$VICOS_CLANG" ]; then
    echo "ERROR: No vicos clang found. Run this on the Jetson."
    exit 1
fi

# --- Hard-float toolchain ---
HF_GCC="arm-linux-gnueabihf-gcc"
if ! command -v "$HF_GCC" &>/dev/null; then
    echo "ERROR: $HF_GCC not found. Install: apt install gcc-arm-linux-gnueabihf"
    exit 1
fi

cd "$SCRIPT_DIR"

echo "=== Building pv_shim (soft-float, replaces libpv_porcupine_softfp.so) ==="
echo "  Toolchain: $VICOS_CLANG"
echo "  Sysroot:   $SYSROOT"

$VICOS_CLANG \
    --sysroot="$SYSROOT" \
    -march=armv7-a -mfloat-abi=softfp -mfpu=neon \
    -shared -fPIC -O2 -Wall -Wextra \
    -Wl,--version-script=pv_shim.version \
    -o libpv_porcupine_softfp.so pv_shim.c

echo "  Built: libpv_porcupine_softfp.so"
file libpv_porcupine_softfp.so
echo ""

echo "=== Building pv_worker (hard-float daemon) ==="
echo "  Toolchain: $HF_GCC"

$HF_GCC \
    -march=armv7-a -mfloat-abi=hard -mfpu=vfpv3 \
    -O2 -Wall -Wextra \
    -Wl,--dynamic-linker=/anki/lib/hf/ld-linux-armhf.so.3 \
    -Wl,-rpath,/anki/lib/hf \
    -o pv_worker pv_worker.c -ldl

echo "  Built: pv_worker"
file pv_worker
echo ""

echo "=== Build successful ==="
ls -la libpv_porcupine_softfp.so pv_worker
echo ""
echo "Deploy to Vector (mount -o remount,rw / first):"
echo "  scp libpv_porcupine_softfp.so root@192.168.1.73:/anki/lib/libpv_porcupine_softfp.so"
echo "  scp pv_worker root@192.168.1.73:/anki/bin/pv_worker"
echo "  scp pv_worker.service root@192.168.1.73:/etc/systemd/system/pv_worker.service"
echo ""
echo "First-time setup (hard-float runtime in /anki/lib/hf/):"
echo "  Required: ld-linux-armhf.so.3, libc.so.6, libm.so.6, libdl.so.2,"
echo "            libpthread.so.0, libgcc_s.so.1, liblog.so (stub), libpv_porcupine.so"
echo "  Source: RPi ARM11 Porcupine v4 package + system hard-float libs from /usr/lib/"
echo ""
echo "  systemctl daemon-reload && systemctl enable pv_worker && systemctl start pv_worker"
