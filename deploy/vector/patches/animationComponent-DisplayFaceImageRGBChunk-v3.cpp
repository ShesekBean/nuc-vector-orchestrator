// Patch v3 for engine/components/animationComponent.cpp
// Fixes DisplayFaceImageRGB on Vector 2.0 (Xray, 160x80 screen)
//
// Applied to: wire-os-victor commit 852b6781 (WireOS 3.0.1.32)
// File: engine/components/animationComponent.cpp
// Function: AnimationComponent::HandleMessage(ExternalInterface::DisplayFaceImageRGBChunk)
//
// This replaces the original handler (~lines 895-923) with:
// 1. Static ImageRGB565 buffer (avoids heap corruption from alloc/dealloc cycle)
// 2. Stride conversion (184 → FACE_DISPLAY_WIDTH) for Xray's 160x80 screen
// 3. Goes through DisplayFaceImage() which properly calls EnableKeepFaceAlive()
//    in vic-anim to suppress eye animations during display
//
// Build: cd /tmp/victor-build/victor/_build/vicos/Release && ccache -C && ninja vic-engine
// Deploy: scp lib/libcozmo_engine.so root@192.168.1.73:/anki/lib/libcozmo_engine.so
//         ssh root@192.168.1.73 "systemctl restart vic-robot"

template<>
void AnimationComponent::HandleMessage(const ExternalInterface::DisplayFaceImageRGBChunk& msg)
{
  if (!_isInitialized) {
    return;
  }

  // Collect all chunks from the Go gateway (always 30 chunks, stride 184).
  _oledImageBuilder->AddDataChunk(msg.faceData, msg.chunkIndex, msg.numPixels);

  u32 fullMask = 0;
  for( int i=0; i<msg.numChunks; ++i ) {
    fullMask |= (1L << i);
  }

  if( (_oledImageBuilder->GetRecievedChunkMask() ^ fullMask) == 0 )
  {
    // All chunks received. Use static buffer to avoid heap corruption
    // (the original non-static version crashed after 2-3 images).
    static Vision::ImageRGB565 img565(FACE_DISPLAY_HEIGHT, FACE_DISPLAY_WIDTH);

    const auto& srcData = _oledImageBuilder->GetAllData();
    u16* dst = img565.GetRawDataPointer();

    // Stride conversion: Go gateway sends data with stride 184 (SDK resolution),
    // but Xray (Vector 2.0) has 160x80 screen. Copy with correct stride.
    // For Vector 1.0 (184x96), srcStride == FACE_DISPLAY_WIDTH so this is a
    // simple linear copy — no performance penalty.
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
