# DisplayFaceImageRGB — Root Cause, Fix & Xray Screen Discovery

## Problem

`DisplayFaceImageRGB` gRPC call on Vector 2.0 OSKR firmware had **two** issues:
1. **Heap corruption crash** after 2-3 images (Error 914/915)
2. **Wrong screen resolution** — SDK assumes 184×96 but Vector 2.0 (Xray) has 160×80

**GitHub Issue:** #129
**Status:** FIXED (2026-03-13)

---

## Root Cause 1: Heap Corruption

The `AnimationComponent::HandleMessage(DisplayFaceImageRGBChunk)` handler:

1. Assembled 30 incoming chunks into an `ImageRGB565` (backed by `cv::Mat` — heap allocation)
2. Re-chunked the image into 30 `RobotInterface::DisplayFaceImageRGBChunk` messages
3. Sent each via `_robot->SendMessage()` (allocates `std::vector<uint8_t>` per message)
4. Destroyed the `ImageRGB565` (heap deallocation)

After 2-3 cycles: `double free or corruption`, `malloc(): corrupted top size`.

## Root Cause 2: Xray Screen Resolution

Vector 2.0 is hardware revision "Xray" (`HW_VER >= 0x20`). Confirmed via `emr-cat v` → `00000020`.

| | Vector 1.0 (Victor) | Vector 2.0 (Xray) |
|---|---|---|
| Screen | 184×96 | **160×80** |
| Pixels | 17,664 | **12,800** |
| Chunks | 30 | **22** |
| `IsXray()` | false | **true** |
| Gamma | 1.0 | 2.1 |

The Go gateway (`vic-cloud`) **hardcodes** `totalPixels = 17664` and always sends 30 chunks. On Xray:
- vic-anim expects 22 chunks (mask `0x3FFFFF`)
- After chunk 21, image triggers — but with stride-184 data interpreted as stride-160 (garbled)
- Chunks 22-29 **overflow** vic-anim's 12800-pixel buffer (writes past end!)
- Extra chunks **pollute the chunk mask**, preventing ALL subsequent images from ever completing

The SDK also validates `image_width == 184` and `image_height == 96`, enforcing the wrong resolution.

### Why Mirror Mode Works

Mirror mode creates images at the correct 160×80 resolution INSIDE vic-engine (using `_screenImg` member variable sized to `FACE_DISPLAY_HEIGHT × FACE_DISPLAY_WIDTH`). It never goes through the Go gateway. It sends 22 chunks directly to vic-anim via `DisplayFaceImageHelper`, which calls `EnableKeepFaceAlive(false, duration_ms)` to suppress eye animations.

### Why Everything Else Worked

| Method | Path | Heap Allocs | Stride | Works? |
|--------|------|-------------|--------|--------|
| **Mirror Mode** | `_screenImg` member (160×80, allocated once) | 0 per frame | Correct | YES |
| **DisplayFaceImage(ImageRGB)** | `static ImageRGB565` (Anki comment: "static to avoid repeatedly allocating") | 0 per call | Correct | YES |
| **DisplayFaceImageBinaryChunk** | Direct forward to vic-anim | 1 per chunk | N/A | YES |
| **DisplayFaceImageRGBChunk (original)** | Assemble→cv::Mat alloc→re-chunk→dealloc | 31+ per image | Wrong (184) | **CRASH** |
| **DisplayFaceImageRGBChunk (v2 direct-forward)** | Forward chunks directly to vic-anim | 0 | Wrong (184) | **GARBLED + mask corruption** |

---

## Fix (v3) — Static Buffer + Stride Conversion

Changed `engine/components/animationComponent.cpp`:

```cpp
// BEFORE (original — crashes):
template<>
void AnimationComponent::HandleMessage(const ExternalInterface::DisplayFaceImageRGBChunk& msg)
{
  _oledImageBuilder->AddDataChunk(msg.faceData, msg.chunkIndex, msg.numPixels);
  // ... wait for all 30 chunks ...
  Vision::ImageRGB565 image(FACE_DISPLAY_HEIGHT, FACE_DISPLAY_WIDTH, _oledImageBuilder->GetAllData());
  DisplayFaceImage(image, msg.duration_ms, msg.interruptRunning);  // re-chunks into 30 messages
  _oledImageBuilder->Clear();
}

// AFTER (v3 — fixed):
template<>
void AnimationComponent::HandleMessage(const ExternalInterface::DisplayFaceImageRGBChunk& msg)
{
  if (!_isInitialized) { return; }

  // Collect all chunks from the Go gateway (always 30 chunks, stride 184).
  _oledImageBuilder->AddDataChunk(msg.faceData, msg.chunkIndex, msg.numPixels);

  u32 fullMask = 0;
  for( int i=0; i<msg.numChunks; ++i ) {
    fullMask |= (1L << i);
  }

  if( (_oledImageBuilder->GetRecievedChunkMask() ^ fullMask) == 0 )
  {
    // Static buffer avoids heap corruption (original non-static crashed after 2-3 images).
    static Vision::ImageRGB565 img565(FACE_DISPLAY_HEIGHT, FACE_DISPLAY_WIDTH);

    const auto& srcData = _oledImageBuilder->GetAllData();
    u16* dst = img565.GetRawDataPointer();

    // Stride conversion: Go gateway sends data with stride 184 (SDK resolution),
    // but Xray (Vector 2.0) has 160x80 screen. Copy with correct stride.
    // For Vector 1.0 (184x96), srcStride == FACE_DISPLAY_WIDTH — simple linear copy.
    const int srcStride = 184;
    for (int row = 0; row < FACE_DISPLAY_HEIGHT; ++row) {
      const int srcOffset = row * srcStride;
      const int dstOffset = row * FACE_DISPLAY_WIDTH;
      for (int col = 0; col < FACE_DISPLAY_WIDTH; ++col) {
        dst[dstOffset + col] = srcData[srcOffset + col];
      }
    }

    DisplayFaceImage(img565, msg.duration_ms, msg.interruptRunning);
    _oledImageBuilder->Clear();
  }
}
```

### Why This Works

1. **Static `ImageRGB565`** — allocated once, reused forever. No heap alloc/dealloc cycle.
2. **Stride conversion** — copies 160 pixels per row from 184-wide source data, producing correct 160×80 image.
3. **Goes through `DisplayFaceImage()`** — which calls `DisplayFaceImageHelper` → sends 22 correct chunks → vic-anim calls `EnableKeepFaceAlive(false, duration_ms)` to suppress eyes.
4. **`_oledImageBuilder` buffer is 17664** (hardcoded in `RGB565ImageBuilder`, NOT Xray-dependent) — safely holds all 30 incoming chunks.
5. **Re-chunks into 22** — `DisplayFaceImageHelper` uses `FACE_DISPLAY_NUM_PIXELS` (12800 for Xray), producing exactly 22 chunks that fit vic-anim's buffer.

### Patch History

| Version | Approach | Result |
|---------|----------|--------|
| v1 (static only) | Static ImageRGB565, assemble + re-chunk | Deployed wrong binary (`vic-engine` launcher, not `libcozmo_engine.so`) |
| v2 (direct-forward) | Forward chunks directly to vic-anim | No crash, but garbled image + mask corruption on Xray |
| **v3 (static + stride)** | Static buffer + stride 184→160 conversion + re-chunk | **WORKS** — correct image, no crash, eyes suppressed |

---

## Deployment

The fix is in `libcozmo_engine.so` (22MB shared library), NOT `vic-engine` (260KB launcher).

```bash
# Build on Jetson (192.168.1.70)
cd /tmp/victor-build/victor/_build/vicos/Release && ninja vic-engine

# Deploy to Vector (remount RW first!)
ssh vector 'mount -o remount,rw /'
scp lib/libcozmo_engine.so root@192.168.1.73:/anki/lib/libcozmo_engine.so
ssh root@192.168.1.73 "systemctl restart vic-robot"
```

### Binary Hashes

| File | Original (WireOS 3.0.1.32) | v2 (direct-forward) | v3 (static+stride) |
|------|---------------------------|---------------------|---------------------|
| `/anki/lib/libcozmo_engine.so` | `1fc05d379eaa9efb657ea1367ce7a510` | `347151a029312e3cb8a2e543f65ffc05` | `2a0aabd3fd4d1eb160efc719ddafe86b` |
| `/anki/lib/libcozmo_engine.so.orig` | (backup of original) | — | — |
| `/anki/bin/vic-engine` | `bf64c36e0d4a8a5b392d391d56ed77f4` | unchanged (launcher) | unchanged (launcher) |
| `/anki/bin/vic-anim` | `e0bd56133b4b1f71c586cf05a80a6a10` | unchanged | unchanged |

### Test Results

- **Original code:** 2/6 images, crash at image 3 (100% reproducible)
- **v2 (direct-forward):** No crash, but garbled green image, eyes not suppressed
- **v3 (static+stride):** Full-color images display correctly, eyes suppressed, continuous operation

---

## Build Environment (on Jetson)

### Source
- Repo: `os-vector/wire-os-victor` at commit `852b6781226e057534c4dbef87c8b1134f8b64e0` (WireOS 3.0.1.32)
- Location on Jetson (192.168.1.70): `/tmp/victor-build/victor/`
- Toolchain: vicos-sdk 5.3.0-r07 Clang (ARM32 cross-compile on aarch64 Jetson)
- Build: `cd _build/vicos/Release && ninja vic-engine`
- Clear ccache before rebuilding: `ccache -C`

### Key Source Files

| File | Component | Role |
|------|-----------|------|
| `engine/components/animationComponent.cpp` | vic-engine (libcozmo_engine.so) | **PATCHED (v3)** — face image chunk handler |
| `animProcess/src/cozmoAnim/animation/animationStreamer.cpp` | vic-anim | Chunk reassembly + display + `EnableKeepFaceAlive` |
| `engine/vision/mirrorModeManager.cpp` | vic-engine | Mirror mode (reference: correct pattern) |
| `cloud/cloud/message_handler.go` | vic-cloud | gRPC→CLAD chunking (Go, hardcodes 184×96) |
| `robot/include/anki/cozmo/shared/cozmoConfig.h` | shared | `FACE_DISPLAY_WIDTH/HEIGHT` (160×80 for Xray) |
| `robot/include/anki/cozmo/shared/factory/emrHelper_vicos.h` | shared | `IsXray()` = `HW_VER >= 0x20` |
| `coretech/vision/shared/rgb565Image/rgb565ImageBuilder.h` | shared | `PIXEL_COUNT = 17664` (hardcoded, NOT Xray-dependent) |

---

## Vector 2.0 (Xray) Display Notes

- **Screen resolution:** 160×80 (NOT 184×96 as SDK assumes)
- **SDK validation:** Enforces 184×96 — send 184×96 images, vic-engine handles stride conversion
- **Pixel format:** RGB565 (2 bytes/pixel, 25600 bytes for 160×80)
- **Eye suppression:** `EnableKeepFaceAlive(false, duration_ms)` in vic-anim — must go through `DisplayFaceImage()` path, not direct chunk forwarding
- **Chunk count:** vic-anim expects 22 chunks on Xray (mask `0x3FFFFF`)
- **HW_VER:** `0x20` (confirmed via `emr-cat v` on robot)
- **Gamma:** 2.1 (vs 1.0 on Vector 1.0)

## KeepFaceAlive Bug & Bridge Workaround

`DisplayFaceImage()` calls `EnableKeepFaceAlive(false, duration_ms)` in vic-anim
(`animationStreamer.cpp:749`), which permanently sets a static flag
`s_enableKeepFaceAlive = false`. This flag is **never restored** — the only code
paths that call `EnableKeepFaceAlive(true)` are `SelfTestEnd` and `ExitCCScreen`.

This means after any `set_screen_with_image_data()` call, Vector's face animation
(eyes, blinks, darts) is permanently disabled until vic-anim restarts.

**Attempted fixes that didn't work:**
1. Patching vic-anim to call `RemoveKeepFaceAlive` without touching the flag — image still persists because `SetFaceImage` sprite stays
2. Setting `s_enableKeepFaceAlive = true` immediately after `EnableKeepFaceAlive(false)` — `KeepFaceAlive()` only adds overlay layers, doesn't replace the `SetFaceImage` sprite
3. `say_text(" ")` from the bridge connection — doesn't restore face (override priority issue)

**Working fix (bridge software):** After the hold thread finishes, release and
re-acquire SDK behavior control via `robot.conn.release_control()` /
`robot.conn.request_control()`. This forces a full animation pipeline reset.

Code: `apps/vector/bridge/routes.py` → `_restore_face_animation()`.

## Vector SSH Access

```bash
ssh -i ~/.ssh/wireos_ssh_key -o PubkeyAcceptedAlgorithms=+ssh-rsa root@192.168.1.73
```

## Key Paths on Robot

| Path | Contents |
|------|----------|
| `/anki/lib/libcozmo_engine.so` | Engine shared library (22MB, ARM) — **PATCHED v3** |
| `/anki/lib/libcozmo_engine.so.orig` | Original engine library (backup) |
| `/anki/bin/vic-engine` | Engine launcher (260KB, ARM) |
| `/anki/bin/vic-anim` | Animation process binary (3.9MB, ARM) |
| `/anki/bin/vic-cloud` | Cloud/gateway binary (5MB, Go) |
