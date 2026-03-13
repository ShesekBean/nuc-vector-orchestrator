# DisplayFaceImageRGB Crash Research

## Problem

`DisplayFaceImageRGB` gRPC call crashes **vic-engine** on Vector 2.0 OSKR firmware (both WireOS 3.0.1.32 and DDL 2.0.1.6091). After 2-3 images, vic-engine crashes with heap corruption. Error 914/915 (NO_ENGINE_PROCESS / NO_ENGINE_COMMS).

**GitHub Issue:** #129

---

## Root Cause — CONFIRMED

**Heap corruption in vic-engine** (C++ binary), NOT vic-anim.

### Evidence

Three crash types observed, all pointing to heap corruption:
1. `double free or corruption (out)` — glibc detects corrupted chunk metadata
2. `malloc(): corrupted top size` — glibc detects corrupted heap top chunk
3. Both occur in vic-engine (logwrapper PID), not vic-anim

### Reproduction

100% reproducible:
1. Connect to Vector via SDK
2. Send 2-3 `DisplayFaceImageRGB` calls (solid color images, 184x96 RGB565)
3. vic-engine crashes with heap corruption
4. vic-anim detects engine is gone → fault code 915

Tested with:
- Behavior control: Still crashes
- Longer delays (5s between images): Still crashes
- Reconnecting between images: Still crashes (even faster)
- With patched vs unpatched vic-anim: Same crash (confirms issue is in vic-engine)

### Data Flow

```
SDK sends DisplayFaceImageRGBRequest via gRPC (port 443)
  → vic-cloud (Go gateway) receives it
  → Go: SendFaceDataAsChunks() splits into 30 CLAD chunks (600 uint16 each)
  → Each chunk sent via UNIX domain socket to vic-engine
  → vic-engine: AnimationComponent::HandleMessage(DisplayFaceImageRGBChunk)
    ├─ _oledImageBuilder->AddDataChunk() — assembles chunks
    ├─ When all 30 received: creates ImageRGB565 (cv::Mat allocation)
    ├─ DisplayFaceImage() → DisplayFaceImageHelper()
    │   └─ Re-chunks into 30 RobotInterface::DisplayFaceImageRGBChunk
    │   └─ Sends each via IPC to vic-anim
    └─ _oledImageBuilder->Clear()

vic-anim receives chunks from engine:
  → AnimationStreamer::Process_displayFaceImageChunk()
  → Reassembles into face image
  → SetFaceImage() → FaceDisplay::DrawToFace() → LCD
```

### Likely Bug Location

The crash is in vic-engine's `AnimationComponent` path. Suspects:
1. **cv::Mat allocation/deallocation** — `ImageRGB565` uses OpenCV `cv::Mat` (via `Array2d<PixelRGB565>`) for heap memory. After 2-3 create/destroy cycles, the heap becomes corrupted. The `cv::Mat` reference-counting destructor may have an off-by-one write.
2. **IPC message serialization** — `_robot->SendMessage(RobotInterface::EngineToRobot(MessageType(msg)))` creates a tagged union on each of 30 chunks. The union's copy/move semantics or serialization may write past allocated bounds.
3. **The `_oledImageBuilder` vector** — `std::vector<uint16_t>` of 17664 elements. `AddDataChunk()` copies chunk data at `chunkIndex * 600` offset. Though bounds appear correct, subtle size mismatch could corrupt adjacent heap metadata.

### NOT the Cause

- **vic-anim race conditions**: Originally suspected, but wrong process. The mutex patch we applied to `_streamingAnimation` / `_proceduralAnimation` in vic-anim was unnecessary — the crash is in vic-engine.
- **Go gateway chunking**: The `SendFaceDataAsChunks` code correctly splits 17664 pixels into 30 chunks of 600 (last chunk: 264). The formula `(totalPixels + faceImagePixelsPerChunk + 1) / faceImagePixelsPerChunk` has a minor math error (`+1` should be `-1`) but produces the correct result (30) for this input.
- **Behavior control**: Crash occurs with or without behavior control.
- **Timing**: Crash occurs with delays of 1s, 3s, or 5s between images.

---

## Workarounds

### 1. Use PlayAnimation Instead (Current Bridge Approach)

The `/display` endpoint in `apps/vector/bridge/routes.py` uses `play_animation` with predefined expressions. This avoids `DisplayFaceImageRGB` entirely and works reliably.

### 2. Limit to 1-2 Images Per Engine Lifecycle

If a single custom image is needed (e.g., status display), send it immediately after boot. After 2 images, consider the API unusable until vic-engine restarts.

### 3. Custom Animation Files (Future)

Create FlatBuffers animation files with embedded face sprites. These go through `PlayAnimation` which is stable.

### 4. Direct LCD Access via SPI (Complex)

Vector's LCD is connected via SPI (`/dev/spidev0.0`). The `lcd_draw_frame2()` function in `core/lcd.c` writes to SPI. This would bypass all engine/anim processes but requires reverse-engineering the SPI protocol and display controller commands.

---

## What Works Without Patching

| Method | Works? | Notes |
|--------|--------|-------|
| PlayAnimation | YES | Built-in animations work perfectly |
| SetCustomEyeColor | YES | Changes eye color safely |
| EnableMirrorMode | YES | Shows camera feed on face |
| DisplayFaceImageRGB (1-2 calls) | YES | First 2 images usually display correctly |
| DisplayFaceImageRGB (3+ calls) | NO | Crashes vic-engine with heap corruption |
| Custom FlatBuffers animations | Untested | Should work — goes through animation engine |

---

## Build Environment (on Jetson)

### Source
- Repo: `os-vector/wire-os-victor` at commit `852b6781226e057534c4dbef87c8b1134f8b64e0` (WireOS 3.0.1.32)
- Location on Jetson: `/tmp/victor-build/victor/`
- Toolchain: vicos-sdk 5.3.0 Clang (ARM32 cross-compile on aarch64 Jetson)

### Key Source Files

| File | Component | Role |
|------|-----------|------|
| `animProcess/src/cozmoAnim/animation/animationStreamer.cpp` | vic-anim | Processes face image chunks |
| `animProcess/src/cozmoAnim/faceDisplay/faceDisplay.cpp` | vic-anim | LCD draw thread |
| `animProcess/src/cozmoAnim/faceDisplay/faceDisplayImpl_vicos.cpp` | vic-anim | SPI LCD driver |
| `engine/components/animationComponent.cpp` | vic-engine | Receives/re-chunks face images |
| `coretech/vision/shared/rgb565Image/rgb565ImageBuilder.cpp` | vic-engine | Chunk assembly buffer |
| `cloud/cloud/message_handler.go` | vic-cloud | gRPC→CLAD chunking (Go) |
| `cloud/cloud/ipc_manager.go` | vic-cloud | UNIX domain socket IPC |

### vic-anim Patch Files (No Longer Needed)
- `/tmp/vic-anim-patch/vic-anim` — original binary (MD5: `e0bd56133b4b1f71c586cf05a80a6a10`)
- `/tmp/vic-anim-patch/vic-anim-patched-v2` — recursive_mutex patch (MD5: `90b82e7839c0901669b55c044b4ec612`)
- Original restored to Vector on 2026-03-13

---

## Vector SSH Access

```bash
ssh -i ~/.ssh/wireos_ssh_key -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.1.73
```

## Key Paths on Robot

| Path | Contents |
|------|----------|
| `/anki/bin/vic-anim` | Animation process binary (3.9MB, ARM) |
| `/anki/bin/vic-engine` | Engine binary (309KB, ARM) |
| `/anki/bin/vic-cloud` | Cloud/gateway binary (5MB, Go) |
| `/anki/data/assets/cozmo_resources/` | All assets (animations, sprites, sounds) |
| `/data/tombstones/` | Crash dumps |
| `/dev/spidev0.0` | LCD SPI device |
