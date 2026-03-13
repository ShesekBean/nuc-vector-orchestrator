# DisplayFaceImageRGB Crash — Root Cause & Fix

## Problem

`DisplayFaceImageRGB` gRPC call crashed **vic-engine** on Vector 2.0 OSKR firmware (both WireOS 3.0.1.32 and DDL 2.0.1.6091). After 2-3 images, vic-engine crashed with heap corruption. Error 914/915 (NO_ENGINE_PROCESS / NO_ENGINE_COMMS).

**GitHub Issue:** #129
**Status:** FIXED (2026-03-13)

---

## Root Cause

**Heap corruption in vic-engine** caused by the `AnimationComponent::HandleMessage(DisplayFaceImageRGBChunk)` code path. The handler:

1. Assembled 30 incoming chunks into an `ImageRGB565` (backed by `cv::Mat` — heap allocation)
2. Re-chunked the image into 30 `RobotInterface::DisplayFaceImageRGBChunk` messages
3. Sent each via `_robot->SendMessage()` (which allocates `std::vector<uint8_t>` per message)
4. Destroyed the `ImageRGB565` (heap deallocation)

After 2-3 cycles of this assemble→re-chunk→destroy pattern, glibc detected heap corruption:
- `double free or corruption (out)`
- `malloc(): corrupted top size`
- `malloc(): invalid size (unsorted)`

### Why Everything Else Worked

| Method | Path | Heap Allocs | Works? |
|--------|------|-------------|--------|
| **Mirror Mode** | `_screenImg` member variable (allocated once in constructor) | 0 per frame | YES |
| **DisplayFaceImage(ImageRGB)** | `static ImageRGB565 img565` (Anki's own comment: "static to avoid repeatedly allocating") | 0 per call | YES |
| **DisplayFaceImageBinaryChunk** | Direct forward to vic-anim (no reassembly) | 1 per chunk | YES |
| **DisplayFaceImageRGBChunk** | Assemble→cv::Mat alloc→re-chunk 30 msgs→dealloc | 31+ per image | **CRASH** |

---

## Fix

**Direct-forward chunks to vic-anim** instead of reassembling. Changed `engine/components/animationComponent.cpp`:

```cpp
// BEFORE (buggy): assemble all chunks, create ImageRGB565, re-chunk, send 30 messages
template<>
void AnimationComponent::HandleMessage(const ExternalInterface::DisplayFaceImageRGBChunk& msg)
{
  _oledImageBuilder->AddDataChunk(msg.faceData, msg.chunkIndex, msg.numPixels);
  // ... wait for all 30 chunks ...
  Vision::ImageRGB565 image(FACE_DISPLAY_HEIGHT, FACE_DISPLAY_WIDTH, _oledImageBuilder->GetAllData());
  DisplayFaceImage(image, msg.duration_ms, msg.interruptRunning);  // re-chunks into 30 messages
  _oledImageBuilder->Clear();
}

// AFTER (fixed): forward each chunk directly, like DisplayFaceImageBinaryChunk does
template<>
void AnimationComponent::HandleMessage(const ExternalInterface::DisplayFaceImageRGBChunk& msg)
{
  if (!_isInitialized) { return; }
  _robot->SendRobotMessage<RobotInterface::DisplayFaceImageRGBChunk>(
      msg.duration_ms, msg.faceData, msg.numPixels, 0, msg.chunkIndex);
}
```

This eliminates ALL intermediate heap allocations (cv::Mat, ImageRGB565, 30× vector serialization buffers). vic-anim already has chunk reassembly logic and handles it correctly.

### Deployment

The fix is in `libcozmo_engine.so` (22MB shared library), NOT `vic-engine` (260KB launcher).

```bash
# Build on Jetson (192.168.1.70)
cd /tmp/victor-build/victor/_build/vicos/Release && ninja vic-engine

# Deploy to Vector
scp lib/libcozmo_engine.so root@192.168.1.73:/anki/lib/libcozmo_engine.so
ssh root@192.168.1.73 "systemctl restart vic-robot"
```

### Binary Hashes

| File | Original (WireOS 3.0.1.32) | Patched |
|------|---------------------------|---------|
| `/anki/lib/libcozmo_engine.so` | `1fc05d379eaa9efb657ea1367ce7a510` | `347151a029312e3cb8a2e543f65ffc05` |
| `/anki/lib/libcozmo_engine.so.orig` | (backup of original) | — |
| `/anki/bin/vic-engine` | `bf64c36e0d4a8a5b392d391d56ed77f4` | unchanged (just a launcher) |
| `/anki/bin/vic-anim` | `e0bd56133b4b1f71c586cf05a80a6a10` | unchanged |

### Test Results

- **Before fix:** 2/6 images, crash at image 3 (100% reproducible)
- **After fix:** 12/12 images across 2 consecutive runs, zero crashes

---

## Build Environment (on Jetson)

### Source
- Repo: `os-vector/wire-os-victor` at commit `852b6781226e057534c4dbef87c8b1134f8b64e0` (WireOS 3.0.1.32)
- Location on Jetson (192.168.1.70): `/tmp/victor-build/victor/`
- Toolchain: vicos-sdk 5.3.0-r07 Clang (ARM32 cross-compile on aarch64 Jetson)
- Build: `cd _build/vicos/Release && ninja vic-engine`

### Key Source Files

| File | Component | Role |
|------|-----------|------|
| `engine/components/animationComponent.cpp` | vic-engine (libcozmo_engine.so) | **PATCHED** — face image chunk handler |
| `animProcess/src/cozmoAnim/animation/animationStreamer.cpp` | vic-anim | Chunk reassembly + display |
| `engine/vision/mirrorModeManager.cpp` | vic-engine | Mirror mode (reference: correct pattern) |
| `cloud/cloud/message_handler.go` | vic-cloud | gRPC→CLAD chunking (Go) |

---

## Vector SSH Access

```bash
ssh -i ~/.ssh/wireos_ssh_key -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.1.73
```

## Key Paths on Robot

| Path | Contents |
|------|----------|
| `/anki/lib/libcozmo_engine.so` | Engine shared library (22MB, ARM) — **PATCHED** |
| `/anki/lib/libcozmo_engine.so.orig` | Original engine library (backup) |
| `/anki/bin/vic-engine` | Engine launcher (260KB, ARM) |
| `/anki/bin/vic-anim` | Animation process binary (3.9MB, ARM) |
| `/anki/bin/vic-cloud` | Cloud/gateway binary (5MB, Go) |
