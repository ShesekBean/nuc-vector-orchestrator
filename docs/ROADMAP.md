# Vector Orchestrator — Roadmap

**Last updated:** 2026-03-10
**Repo:** `ShesekBean/nuc-vector-orchestrator`
**Tracking:** [GitHub Issues](https://github.com/ShesekBean/nuc-vector-orchestrator/issues)

---

## Goal

Bring Vector 2.0 (OSKR) to feature parity with the R3 Jetson robot, with improvements. The NUC handles ALL compute — Vector is a thin gRPC endpoint for hardware (motors, camera, mic, speaker, display, sensors).

## Architecture Overview

```
Signal (Ophir's phone)
    │
    ▼
OpenClaw (Vector agent) on NUC ←── Voice (Vector mic → OpenClaw Talk Mode)
    │
    ▼
HTTP→gRPC Bridge (NUC) ── gRPC ──► Vector 2.0
    │
    ├── Vision pipeline (YOLO, face rec, scene) ← camera frames
    ├── Voice pipeline (OpenClaw Talk Mode) ← mic audio
    ├── Motion planner (PD controller) → motor commands
    └── LiveKit bridge → WebRTC to Ophir's phone
```

**Key architectural decisions:**
- **No ROS2** — Vector SDK events + lightweight NUC event bus (hybrid)
- **OpenVINO** for inference (Intel i7-1360P + Iris Xe, no CUDA)
- **OpenClaw Talk Mode** for voice (gpt-4o-transcribe STT + OpenAI TTS, all via OAuth)
- **Differential drive** planner (tank treads, no strafing — turn-then-drive)
- **wire-pod** on NUC replaces Anki cloud services

---

## Phases

### Phase 0: Infrastructure (no robot needed)

Set up NUC services and tooling before Vector hardware arrives.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 1 | [Set up wire-pod on NUC](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/1) | None | stuck | Install wire-pod to replace Anki cloud. Handles auth, intent engine, voice processing. |
| 2 | [OSKR unlock Vector + SSH access](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/2) | #1 | stuck | Unlock Vector's Linux OS for root access. Requires wire-pod running first. |
| 29 | [Install OpenVINO on NUC](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/29) | None | ready | Install Intel's ML runtime for YOLO, face recognition. Can do today. |
| 30 | [PGM auto-unstick](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/30) | None | ready | PGM removes `stuck` label when dependency issues close, adds `assigned:worker`. |
| 36 | [Event bus](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/36) | None | ready | Lightweight NUC pub/sub (~100 lines Python) for inter-component events. |
| 39 | [Connection config + service discovery](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/39) | #2 | stuck | Vector IP, gRPC port, auth config. Needs OSKR unlock first. |

**Phase 0 unlocks:** Everything. No other phase can start until wire-pod (#1) and OSKR (#2) are done.

---

### Phase 1: Basic Control (requires robot)

Establish gRPC communication and control each Vector subsystem independently.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 3 | [gRPC connectivity test](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/3) | #1, #2 | stuck | Verify NUC ↔ Vector gRPC works. Health check endpoint. Gate for all hardware issues. |
| 4 | [LED control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/4) | #3 | stuck | `SetBackpackLights` — RGB backpack LEDs. |
| 5 | [Head servo control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/5) | #3 | stuck | `SetHeadAngle` — -22° to 45°. Used by person following. |
| 6 | [Lift control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/6) | #3 | stuck | `SetLiftHeight` — motorized forklift. New capability (R3 had none). |
| 7 | [Motor control + diff drive planner](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/7) | #3 | stuck | `DriveWheels`, `DriveStraight`, `TurnInPlace`. Differential drive (no strafe). |
| 8 | [Battery monitor](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/8) | #3, #36 | stuck | `BatteryState` gRPC — voltage, charging, level. Publishes to event bus. |
| 9 | [Touch + cliff sensors](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/9) | #3, #36 | stuck | `RobotState` stream — capacitive touch, 4 cliff sensors. Safety-critical. |
| 29 | [OpenVINO runtime](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/29) | None | ready | Intel ML inference. Required by YOLO (#11) and face rec (#12). |
| 32 | [HTTP→gRPC bridge](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/32) | #3, #4, #5, #6, #7 | stuck | Central dispatcher on NUC. OpenClaw skills and voice both route through this. |
| 33 | [Standalone test scripts](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/33) | #3 | stuck | One script per subsystem for manual verification. R3 lesson: standalone-first. |
| 35 | [Process supervisor](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/35) | #3, #32, #36, #39 | stuck | Startup, lifecycle, graceful shutdown for all Vector services. |

**Phase 1 unlocks:** Vision (#10+), voice (#18+), Signal integration (#23+).

---

### Phase 2: Vision Pipeline

Stream camera frames to NUC for ML inference.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 10 | [Camera streaming](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/10) | #3, #36 | stuck | `CameraFeed` gRPC stream, 640x360 JPEG frames. Gate for all vision features. |
| 11 | [YOLO person detection](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/11) | #10, #29, #36 | stuck | YOLOv8n via OpenVINO on NUC. ~15fps. Publishes detections to event bus. |
| 12 | [Face recognition](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/12) | #10 | stuck | YuNet detection + SFace embeddings via OpenVINO. Identifies known faces. |
| 13 | [Scene description](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/13) | #10, #11 | stuck | LLM-based scene description from camera frame + YOLO detections. |
| 14 | [Face display (OLED)](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/14) | #3 | stuck | `DisplayImage` gRPC — 184x96 OLED. Expressions, status, emoji. New capability. |

**Phase 2 unlocks:** Person following (#15), obstacle avoidance (#17), intercom photo (#24).

---

### Phase 3: Person Following

Autonomous person tracking and following — the flagship feature.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 15 | [Person following](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/15) | #7, #11, #5, #36, #38, #33 | stuck | YOLO detection → PD controller → differential drive. Core feature. |
| 16 | [Head tracking](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/16) | #5, #11, #15 | stuck | Keep person centered in frame by adjusting head angle during follow. |
| 17 | [Obstacle avoidance](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/17) | #9, #11, #15 | stuck | Camera-based (no LiDAR). Cliff sensors for edges. Safety overlay on follow. |
| 38 | [Kalman filter](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/38) | #11 | stuck | Smooth YOLO detections, predict position between frames. R3 lesson: critical for tracking. |

**Phase 3 unlocks:** Full autonomous following behavior. Combined with voice = "follow me" command.

---

### Phase 4: Voice Pipeline

Voice interaction through OpenClaw Talk Mode — Vector agent handles everything.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 18 | [Mic audio streaming](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/18) | #3 | stuck | Vector mic audio to NUC. SDK `AudioFeed` only returns metadata, not PCM. ALSA arecord doesn't work (mics go through ADSP, not kernel). Requires custom binary using `libaudio_engine.so` — see "Custom Vector Binaries" section. |
| 19 | [Wake word detection](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/19) | #18 | stuck | OpenWakeWord "hey jarvis" on NUC. Gates audio send to OpenClaw. |
| 20 | [OpenClaw Talk Mode integration](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/20) | #18, #19, #3 | stuck | **Core voice issue.** Pipes mic audio → OpenClaw Talk Mode (gpt-4o-transcribe STT → Vector agent → say_text()). All via OpenAI OAuth, zero API cost. Solves accent problem. |
| 21 | [TTS via say_text()](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/21) | #3, #20 | stuck | Use Vector's built-in say_text() for speech output — no OpenAI TTS or PlayAudio needed. |
| 22 | [Voice command routing](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/22) | #20, #23, #32 | stuck | No custom router — Vector agent handles all commands via existing skills. Voice and Signal share the same routing. |
| 37 | [Echo cancellation](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/37) | #18, #19, #21 | stuck | Pause mic during say_text() (blocking call) + hold-off. Prevents wake word re-trigger loop. |

#### Voice Architecture Decision

**Problem:** Ophir's accent causes poor Whisper transcription.
**Solution:** Use OpenClaw Talk Mode with `gpt-4o-transcribe` (2.46% WER, superior accent handling) instead of local Whisper. All through OpenAI OAuth — zero cost.

```
Vector mic ──gRPC──► NUC wake word (OpenWakeWord)
                      │
                      └── triggered ──► OpenClaw Talk Mode
                                         ├── STT: gpt-4o-transcribe (accent-friendly)
                                         ├── Agent: Vector agent (all skills work)
                                         └── text response
                                         │
                                         ▼
                              say_text(response) ──► Vector speaker
```

**Key insight:** Voice commands go through the same Vector agent as Signal DMs. No separate command router to maintain. Any new skill added to OpenClaw automatically works via voice too.

**Future option:** If latency or quality needs improvement, GPT-4o Realtime API (audio-to-audio, no text intermediate, sub-second) — PR #25465 on openclaw/openclaw.

**Phase 4 unlocks:** Full voice interaction, voice-to-Signal (#25).

---

### Phase 5: Signal / OpenClaw Integration

Connect Vector to Ophir via Signal messenger through OpenClaw.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 23 | [Port robot-control skill](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/23) | #32 | stuck | Update OpenClaw skill: HTTP curl → NUC bridge (localhost, not Jetson 192.168.1.71). |
| 24 | [Intercom: photo + text](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/24) | #32, #13 | stuck | Capture photo, send to Ophir via Signal DM with scene description. |
| 25 | [Intercom: voice to Signal](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/25) | #20, #22, #24 | stuck | "Tell Ophir I'm heading out" → Vector agent sends Signal DM. |
| 31 | [LiveKit WebRTC session](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/31) | #10, #32 | done | Live video + one-way audio (user → Vector speaker, 20x amplified, 48→16kHz downsample). Triggered via "robot call me" or `/call/join-url` endpoint. Vector mic → LiveKit blocked on #18 (custom binary needed). Speaker playback disabled during calls (stream_wav_file blocks camera). |
| 34 | [Physical test framework](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/34) | #32, #23 | stuck | Signal-based test checkpoints for physical robot verification. |

**Phase 5 unlocks:** Full remote operation — control Vector via Signal or voice, see what it sees.

---

### Phase 6: Advanced Features

Nice-to-haves and new capabilities unique to Vector.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 26 | [Visual SLAM](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/26) | #10, #7 | stuck | Camera-only SLAM (no LiDAR). Dead reckoning + visual landmarks. |
| 28 | [Multi-modal expressions](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/28) | #14, #4, #21 | stuck | Coordinated face display + LEDs + sound for robot emotions/status. |
| 40 | [Cube interaction](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/40) | #6, #7, #36 | stuck | Detect and pick up Vector's light cube. New capability. |

---

## Dependency Graph

```
Phase 0 (infra)                    Phase 1 (basic)
┌──────────┐                       ┌───────────────┐
│ #1 wire  │──► #2 OSKR ──► #39 ──►│ #3 gRPC test  │
│    pod   │                       │   (GATE)       │
└──────────┘                       └───────┬───────┘
                                      ┌────┼────┬────┬────┐
#29 OpenVINO (standalone)             │    │    │    │    │
#30 PGM auto-unstick (standalone)     ▼    ▼    ▼    ▼    ▼
#36 Event bus (standalone)           #4   #5   #6   #7   #18
                                    LED  head  lift motor  mic
                                     │    │    │    │      │
                                     └──┬─┘    │    │      ▼
                                        │      │    │    #19 wake
                                        ▼      │    │      │
                                    #32 bridge◄┘    │      ▼
                                        │          │    #20 Talk Mode
                                        │          │      │
                              ┌─────────┼──────────┘      ▼
                              │         │              #21 TTS+play
                              │         ▼                  │
                              │    #10 camera              ▼
                              │      │    │            #22 routing
                              │      ▼    │                │
                              │    #11 YOLO◄─#29           │
                              │      │    │                ▼
                              │      ▼    ▼            #25 voice→Signal
                              │    #38  #12 face
                              │      │
                              │      ▼
                              │    #15 follow──►#16 head track
                              │      │
                              │      ▼
                              │    #17 obstacle
                              │
                              ├──► #23 skill port──►#34 test framework
                              ├──► #24 intercom photo
                              └──► #31 LiveKit
```

## Issues That Can Start Today (No Robot)

| # | Issue | Why |
|---|-------|-----|
| 29 | OpenVINO runtime | NUC-only install, no Vector needed |
| 30 | PGM auto-unstick | Control plane code, no hardware |
| 36 | Event bus | Pure NUC Python, ~100 lines |

## Interaction Model

All interactions can be initiated two ways:

1. **Voice** — "Hey Vector, [command]" → Vector mic → OpenClaw Talk Mode → Vector agent → action
2. **Signal** — text Vector agent on Signal DM → OpenClaw agent → robot-control skill → action

Both paths go through the same Vector agent with the same skills. Commands available:

| Command | Voice example | Signal example | Issues |
|---------|--------------|----------------|--------|
| Follow | "follow me" | "robot follow" | #15, #22, #23 |
| Stop | "stop" | "robot stop" | #7, #22, #23 |
| Photo | "take a photo" | "robot photo" | #24 |
| Scene | "what do you see" | "robot scene" | #13 |
| Move | "go forward" | "robot forward" | #7, #22, #23 |
| Balance | "what's my balance" | "monarch balances" | #22 (voice), OpenClaw (Signal) |
| Message | "tell Ophir hello" | (already in Signal) | #25 |
| Call | "call Ophir" | "robot call" | #31 |
| Status | "how's your battery" | "robot status" | #8 |

## Labels

| Label | Meaning |
|-------|---------|
| `component:vector` | Vector robot / gRPC / inference code |
| `stuck` | Blocked — needs hardware, human, or external dependency |
| `assigned:worker` | Ready for automated worker dispatch |
| `phase:0-infra` | Infrastructure (no robot needed) |
| `phase:1-basic` | Basic gRPC control |
| `phase:2-vision` | Vision pipeline |
| `phase:3-follow` | Person following |
| `phase:4-voice` | Voice pipeline |
| `phase:5-signal` | Signal / OpenClaw integration |
| `phase:6-advanced` | Advanced features |
| `blocker:needs-human` | Needs physical test or human decision |
| `milestone` | Something notable working |

## Automated Issue Lifecycle

1. Issues start with `stuck` label (blocked on robot hardware)
2. When dependencies close, PGM (#30) removes `stuck` and adds `assigned:worker`
3. Agent loop picks up `assigned:worker` issues
4. Worker handles full lifecycle: design → code → test → PR → merge
5. PGM notifies Ophir on closures, blockers, milestones

---

### Custom Vector Binaries (cross-compiled for Snapdragon 212)

Custom native binaries to run on Vector, enabling capabilities not available through the SDK or standard ALSA.

| Feature | Why Custom Binary | Details |
|---------|-------------------|---------|
| **Mic audio streaming** | Vector's 4 DMIC mics are accessed through Qualcomm ADSP via `libaudio_engine.so` (FastRPC). The SDK's `AudioFeed` only returns metadata (signal_power), not raw PCM. Standard ALSA `arecord` cannot capture — TERT_MI2S (internal codec) gives white noise (no mics wired to ADC), QUAT_MI2S (external codec) gives I/O error (ADSP clocks not initialized through kernel ALSA). vic-anim owns the mic through ADSP, bypassing the kernel entirely. | Binary uses `libaudio_engine.so` to open mic via ADSP FastRPC, streams 16kHz PCM over TCP socket to NUC. Enables continuous mic audio for LiveKit calls and voice processing without wake word dependency. |
| **Display arbitrary image** | SDK's `DisplayImage` gRPC accepts raw image data but the current API may have limitations for custom images. A native binary could write directly to the 184x96 OLED framebuffer for full control over what's displayed. | Binary accepts image data (e.g., via stdin or TCP) and writes to OLED display hardware. Enables custom faces, status screens, QR codes, etc. |

**Build environment:**
- Target: ARM (Qualcomm Snapdragon 212 / MSM8909, 32-bit ARMv7)
- Cross-compiler: `arm-linux-gnueabihf-gcc` or Qualcomm/Yocto SDK toolchain
- Vector OS: Yocto Linux (busybox userland)
- Key libraries on Vector: `/anki/lib/libaudio_engine.so` (2.8MB, ADSP mic access), `/anki/lib/libAudioPlayer.so`, `/anki/lib/libutil_audio.so`
- Deploy: SCP to Vector, run via SSH

**Research done (2026-03-12):**
- Confirmed mic path: Physical DMICs → QUAT_MI2S → External codec → ADSP → vic-anim (libaudio_engine.so via `/dev/adsprpc-smd`)
- vic-anim does NOT use ALSA for mic capture (only `/dev/snd/pcmC0D0p` for speaker playback)
- Audio already leaves Vector as beamformed Opus via vic-cloud → wire-pod gRPC (port 443), but only after wake word
- See memory file `vector_mic_audio_research.md` for full ALSA debugging results
- `/etc/audio_platform_info.xml` confirms QUAT_MI2S with external codec for built-in mic
- `/etc/mixer_paths_msm8909_pm8916.xml` (active) routes audio-record to TERT_MI2S (wrong, no mics)
- `/etc/mixer_paths_wcd9326_i2s.xml` (not loaded) has correct QUAT_MI2S routing

---

## R3 Lessons Applied

Key lessons from R3 (nuc-orchestrator) baked into this plan:

1. **Standalone-first** — each subsystem has standalone test scripts (#33) before integration
2. **State machines** — follow behavior uses explicit states (SEARCHING → ACQUIRING → FOLLOWING → LOST)
3. **Kalman filter** — smooth YOLO detections, predict between frames (#38)
4. **Echo cancellation** — TTS→mic feedback loop prevention from day 1 (#37)
5. **Gain re-tuning** — PD controller gains must be re-tuned for Vector's differential drive (not mecanum)
6. **int16 audio** — openwakeword requires int16, NOT float32
7. **No over-engineering** — event bus is ~100 lines, not ROS2
8. **Process supervisor** — graceful startup/shutdown, not ad-hoc scripts (#35)

See `docs/vector/oskr-research.md` for full portability matrix.
