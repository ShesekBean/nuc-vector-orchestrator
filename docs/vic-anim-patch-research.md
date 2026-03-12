# vic-anim Firmware Patch Research

## Problem

`DisplayFaceImageRGB` gRPC call crashes vic-engine on Vector 2.0 OSKR firmware 2.0.1.6091 every time. Error 914 (NO_ENGINE_PROCESS) or 915 (NO_ENGINE_COMMS).

**GitHub Issue:** #129

---

## Root Cause

Race condition / use-after-free bug in `AnimationStreamer` (vic-anim process).

### Source Code (digital-dream-labs/vector)

Key files:
- `animProcess/src/cozmoAnim/animation/animationStreamer.cpp` (2157 lines)
- `animProcess/src/cozmoAnim/animation/animationStreamer.h`
- `animProcess/src/cozmoAnim/faceDisplay/faceDisplay.cpp` (283 lines)
- `cannedAnimLib/proceduralFace/proceduralFace.cpp` (787 lines)

### The Bug

1. `_proceduralAnimation` pointer has **NO mutex** protecting it
2. `FaceDisplay::DrawFaceLoop()` runs in a separate thread with its own synchronization
3. When `DisplayFaceImageRGB` arrives, it modifies `_proceduralAnimation` while the draw thread may be using it
4. Source code admits the hack (line 764):
   ```cpp
   // Hack: if _streamingAnimation == _proceduralAnimation, the subsequent
   // CopyIntoProceduralAnimation call will delete *_streamingAnimation
   // without assigning it to nullptr. This assignment prevents associated
   // undefined behavior
   _streamingAnimation = _neutralFaceAnimation;
   ```

### DisplayFaceImageRGB Processing Path

```
Gateway receives DisplayFaceImageRGB gRPC
  → Splits into 30 chunks (600 uint16 each = 17,664 pixels total)
  → Sends via CLAD to vic-anim

AnimationStreamer::Process_displayFaceImageRGBChunk (line 724-754)
  ├─ Accumulates chunks into _faceImageRGB565 buffer
  ├─ When all 30 chunks received (bitmask == 0x3fffffff):
  │   └─ Creates ImageRGBA from RGB565
  │   └─ Wraps in SpriteWrapper shared_ptr
  │   └─ Calls SetFaceImage(handle, false, duration_ms)
  └─ SetFaceImage() (line 838-850):
      └─ Sets face image override on _proceduralAnimation  ← NO LOCK
      └─ If _streamingAnimation != _proceduralAnimation:
         └─ Calls SetStreamingAnimation(_proceduralAnimation, ...)

Meanwhile, in another thread:
AnimationStreamer::Update()
  └─ ExtractAnimationMessages()
     └─ Uses _proceduralAnimation  ← RACE CONDITION
```

### Crash Scenarios

1. **Use-After-Free**: `CopyIntoProceduralAnimation()` calls `SafeDelete(_proceduralAnimation)` while Update() still uses it
2. **Null Pointer**: `_proceduralAnimation` becomes nullptr after delete, assertion fails
3. **Assertion Abort**: vic-engine detects the problem and calls `abort()` via `gsignal` (confirmed in tombstone backtrace)

---

## Proposed Fix

### In animationStreamer.cpp

Add mutex around `_proceduralAnimation` access:

```cpp
// Add member:
std::mutex _proceduralAnimMutex;

// In SetFaceImage():
{
    std::lock_guard<std::mutex> lock(_proceduralAnimMutex);
    _proceduralAnimation->SetFaceImageOverride(spriteWrapper, duration_ms);
    if (_streamingAnimation != _proceduralAnimation) {
        SetStreamingAnimation(_proceduralAnimation, ...);
    }
}

// In Update() / ExtractAnimationMessages():
{
    std::lock_guard<std::mutex> lock(_proceduralAnimMutex);
    // existing code that accesses _proceduralAnimation
}
```

### Alternative: Lock face track before processing

```cpp
// In Process_displayFaceImageRGBChunk, before SetFaceImage:
LockTrack(AnimTrackFlag::FACE_TRACK);
// ... process image ...
// After display completes:
UnlockTrack(AnimTrackFlag::FACE_TRACK);
```

---

## Cross-Compilation Setup

### Target
- CPU: Qualcomm Snapdragon 212 (ARMv7, Cortex-A7)
- OS: Android-based Linux (libc-2.22)
- Binary: ELF 32-bit LSB shared object, ARM EABI5

### Toolchain
```bash
sudo apt install gcc-arm-linux-gnueabihf g++-arm-linux-gnueabihf
```

### Source
```bash
git clone https://github.com/digital-dream-labs/vector.git
```

### Build System
- CMake-based
- Dependencies: OpenCV, FlatBuffers, protobuf, audio libs
- All dependencies need ARM cross-compilation or pre-built ARM libraries

### Dependencies from Vector's /anki/lib/
```
libc-2.22.so, libpthread-2.22.so, libc++.so.1
# Plus whatever vic-anim links against - check with:
# readelf -d /tmp/vic-anim.original | grep NEEDED
```

### Deployment
```bash
# Backup original
ssh root@192.168.1.73 "cp /anki/bin/vic-anim /anki/bin/vic-anim.original"

# Deploy patched binary
scp vic-anim-patched root@192.168.1.73:/anki/bin/vic-anim

# Restart
ssh root@192.168.1.73 "reboot"

# Rollback if broken
ssh root@192.168.1.73 "cp /anki/bin/vic-anim.original /anki/bin/vic-anim && reboot"
```

---

## Crash Evidence from Robot

### Tombstones
- Location: `/data/tombstones/`
- All vic-anim tombstones show SIGSTOP (killed externally by systemd)
- vic-engine tombstone shows `gsignal` (deliberate abort via assertion failure)
- vic-engine d1/d2 registers contained RGB565 pixel data at crash time

### Crash Dumps
- Location: `/data/data/com.anki.victor/cache/`
- Format: `vic-anim-V6091-YYYY-MM-DDTHH-MM-SS-mmm.dmp`
- Many crash dumps from our testing session

### Error Flow
```
vic-engine detects problem → calls abort()/gsignal
  → systemd sees vic-engine exit with failure
  → vic-on-exit writes "914" to /run/fault_code
  → systemd sends SIGSTOP to vic-anim (PartOf=anki-robot.target)
  → Vector displays error 914 on screen
```

---

## What Works Without Patching

| Method | Works? | Notes |
|--------|--------|-------|
| Mirror mode (EnableMirrorMode) | YES | Shows camera feed on face, no crash |
| PlayAnimation | YES | Built-in animations work perfectly |
| SetCustomEyeColor | YES | Changes eye color safely |
| DisplayFaceImageRGB | NO | Crashes vic-engine every time |
| Custom FlatBuffers animations | Untested | Should work — goes through animation engine |

---

## Vector SSH Access

```bash
SSH="ssh -i ~/.ssh/id_rsa_Vector-D2C9 -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.1.73"
```

## Key Paths on Robot

| Path | Contents |
|------|----------|
| `/anki/bin/vic-anim` | Animation process binary (3.9MB, ARM) |
| `/anki/bin/vic-engine` | Engine binary (309KB, ARM) |
| `/anki/bin/vic-cloud` | Cloud/gateway binary (5MB, Go, replaceable) |
| `/anki/etc/vic-engine.env` | Engine config (fault code 914) |
| `/anki/etc/vic-anim.env` | Anim config (fault code 800) |
| `/anki/etc/config/platform_config.json` | Resource paths |
| `/anki/data/assets/cozmo_resources/` | All assets (animations, sprites, sounds) |
| `/anki/data/assets/cozmo_resources/assets/sprites/spriteSequences/` | Face sprite PNGs (184x96, palette mode) |
| `/anki/data/assets/cozmo_resources/assets/animations/` | Animation .bin files (FlatBuffers) |
| `/data/tombstones/` | Crash dumps |
| `/data/data/com.anki.victor/cache/` | Minidump crash files |

## Debug Webserver (NOT available in OSKR build)

- Port 8888: Engine debug webserver (not running)
- Port 8889: Anim debug webserver with ConsoleVars page (not running)
- Config exists at `webServerConfig_anim.json` / `webServerConfig_engine.json`
- Has `consolevarsui.html` — would expose face/animation toggle if running
