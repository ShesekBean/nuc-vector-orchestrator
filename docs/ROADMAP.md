# Vector Orchestrator — Roadmap

**Last updated:** 2026-03-14
**Repo:** `ShesekBean/nuc-vector-orchestrator`
**Tracking:** [GitHub Issues](https://github.com/ShesekBean/nuc-vector-orchestrator/issues)

---

## Current Status

- **~76 issues closed**, 8 phases complete
- All core subsystems operational: vision, voice, person following, Signal integration, LiveKit, expressions, navigation
- Autonomous behaviors active: explorer, smart patrol (Home Guardian), dead reckoning, auto-charge
- Companion behavior system and OpenClaw skills (fitness, finance) deployed
- Vector operates as a fully autonomous home robot with remote control via Signal and voice

---

## Goal

Bring Vector 2.0 (OSKR) to feature parity with the R3 Jetson robot, with improvements. The NUC handles ALL compute — Vector is a thin gRPC endpoint for hardware (motors, camera, mic, speaker, display, sensors).

## Architecture Overview

```
Signal (Ophir's phone)
    │
    ▼
OpenClaw (Vector agent) on NUC ←── Voice (Vector mic → Porcupine wake word → wire-pod Vosk STT)
    │
    ▼
HTTP→gRPC Bridge (NUC) ── gRPC ──► Vector 2.0
    │
    ├── Vision pipeline (YOLO11n OpenVINO, face rec, scene) ← camera frames (800x600 ~15fps)
    ├── Voice pipeline (Porcupine v4 wake word → wire-pod Vosk STT → say_text()) ← mic audio
    ├── Motion planner (PD controller, reactive drive+steer, EMA smoothing) → motor commands
    ├── Navigation (IMU fusion, A* path planning, waypoint management, dead reckoning)
    ├── LiveKit bridge → two-way WebRTC (mic + camera + audio + video) to Ophir's phone
    └── Autonomous behaviors (explorer, Home Guardian patrol, companion)
```

**Key architectural decisions:**
- **No ROS2** — Vector SDK events + lightweight NUC event bus (hybrid)
- **OpenVINO** for inference (Intel i7-1360P + Iris Xe, no CUDA)
- **Porcupine v4** for wake word detection (two-process architecture, free tier)
- **wire-pod Vosk STT** for speech-to-text (local, no cloud dependency)
- **say_text()** for TTS (Vector built-in, no OpenAI TTS needed)
- **Differential drive** planner (tank treads, no strafing — turn-then-drive)
- **wire-pod** on NUC replaces Anki cloud services (stable 24/7)

---

## Phases

### Phase 0: Infrastructure (no robot needed) — COMPLETE

Set up NUC services and tooling before Vector hardware arrives.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 1 | [Set up wire-pod on NUC](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/1) | None | done ✅ | Install wire-pod to replace Anki cloud. Handles auth, intent engine, voice processing. |
| 2 | [OSKR unlock Vector + SSH access](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/2) | #1 | done ✅ | Unlock Vector's Linux OS for root access. |
| 29 | [Install OpenVINO on NUC](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/29) | None | done ✅ | Intel's ML runtime for YOLO, face recognition. |
| 30 | [PGM auto-unstick](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/30) | None | done ✅ | PGM removes `stuck` label when dependency issues close, adds `assigned:worker`. |
| 36 | [Event bus](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/36) | None | done ✅ | Lightweight NUC pub/sub (~100 lines Python) for inter-component events. |
| 39 | [Connection config + service discovery](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/39) | #2 | done ✅ | Vector IP, gRPC port, auth config. |

---

### Phase 1: Basic Control — COMPLETE

Establish gRPC communication and control each Vector subsystem independently.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 3 | [gRPC connectivity test](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/3) | #1, #2 | done ✅ | NUC ↔ Vector gRPC verified. Health check endpoint. |
| 4 | [LED control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/4) | #3 | done ✅ | `SetBackpackLights` — RGB backpack LEDs. |
| 5 | [Head servo control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/5) | #3 | done ✅ | `SetHeadAngle` — -22° to 45°. |
| 6 | [Lift control](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/6) | #3 | done ✅ | `SetLiftHeight` — motorized forklift. |
| 7 | [Motor control + diff drive planner](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/7) | #3 | done ✅ | `DriveWheels`, `DriveStraight`, `TurnInPlace`. Differential drive (no strafe). |
| 8 | [Battery monitor](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/8) | #3, #36 | done ✅ | `BatteryState` gRPC — voltage, charging, level. Publishes to event bus. |
| 9 | [Touch + cliff sensors](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/9) | #3, #36 | done ✅ | `RobotState` stream — capacitive touch, 4 cliff sensors. Safety-critical. |
| 32 | [HTTP→gRPC bridge](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/32) | #3, #4, #5, #6, #7 | done ✅ | Central dispatcher on NUC. OpenClaw skills and voice both route through this. |
| 33 | [Standalone test scripts](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/33) | #3 | done ✅ | One script per subsystem for manual verification. |
| 35 | [Process supervisor](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/35) | #3, #32, #36, #39 | done ✅ | Startup, lifecycle, graceful shutdown for all Vector services. |

---

### Phase 2: Vision Pipeline — COMPLETE

Stream camera frames to NUC for ML inference.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 10 | [Camera streaming](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/10) | #3, #36 | done ✅ | `CameraFeed` gRPC stream, 800x600 frames at ~15fps. |
| 11 | [YOLO person detection](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/11) | #10, #29, #36 | done ✅ | YOLO11n via OpenVINO on NUC. ~15fps. Publishes detections to event bus. |
| 12 | [Face recognition](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/12) | #10 | done ✅ | YuNet detection + SFace embeddings via OpenVINO. Identifies known faces. |
| 13 | [Scene description](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/13) | #10, #11 | done ✅ | LLM-based scene description from camera frame + YOLO detections. |
| 14 | [Face display (OLED)](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/14) | #3 | done ✅ | `DisplayFaceImageRGB` with static buffer + stride conversion fix for Vector 2.0 (Xray) 160x80 OLED. Expressions, status, emoji. |

---

### Phase 3: Person Following — COMPLETE

Autonomous person tracking and following — the flagship feature.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 15 | [Person following](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/15) | #7, #11, #5, #36, #33 | done ✅ | YOLO detection → PD controller (reactive drive+steer, EMA smoothing) → differential drive. Follow pipeline v2. |
| 16 | [Head tracking](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/16) | #5, #11, #15 | done ✅ | Keep person centered in frame by adjusting head angle during follow. |
| 17 | [Obstacle avoidance](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/17) | #9, #11, #15 | done ✅ | Camera-based (no LiDAR). Cliff sensors for edges. Safety overlay on follow. |
| 38 | [Kalman filter](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/38) | #11 | done ✅ | Kalman filter tracker smooths detections at 10Hz. |

---

### Phase 4: Voice Pipeline — COMPLETE

Voice interaction through Porcupine wake word + wire-pod Vosk STT + say_text() TTS.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 18 | [Mic audio streaming](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/18) | #3 | done ✅ | Mic audio streams to NUC via SDK `signal_power` PCM + wire-pod chipper tap. No custom binary needed. |
| 19 | [Wake word detection](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/19) | #18 | done ✅ | Porcupine v4 wake word (two-process architecture, free tier). |
| 20 | [Voice STT integration](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/20) | #18, #19, #3 | done ✅ | Pipes mic audio → wire-pod Vosk STT → Vector agent → say_text(). |
| 21 | [TTS via say_text()](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/21) | #3, #20 | done ✅ | Vector's built-in say_text() for speech output — no OpenAI TTS or PlayAudio needed. |
| 22 | [Voice command routing](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/22) | #20, #23, #32 | done ✅ | Vector agent handles all commands via existing skills. Voice and Signal share the same routing. |
| 37 | [Echo cancellation](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/37) | #18, #19, #21 | done ✅ | Pause mic during say_text() (blocking call) + hold-off. Prevents wake word re-trigger loop. |

#### Voice Architecture

```
Vector mic ──SDK signal_power PCM──► NUC Porcupine v4 wake word (two-process)
                                       │
                                       └── triggered ──► wire-pod Vosk STT
                                                          ├── Transcription (local, no cloud)
                                                          ├── Agent: Vector agent (all skills work)
                                                          └── text response
                                                              │
                                                              ▼
                                                   say_text(response) ──► Vector speaker
```

**Key insight:** Voice commands go through the same Vector agent as Signal DMs. No separate command router to maintain. Any new skill added to OpenClaw automatically works via voice too.

---

### Phase 5: Signal / OpenClaw Integration — COMPLETE

Connect Vector to Ophir via Signal messenger through OpenClaw.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 23 | [Port robot-control skill](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/23) | #32 | done ✅ | OpenClaw skill: HTTP curl → NUC bridge (localhost). |
| 24 | [Intercom: photo + text](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/24) | #32, #13 | done ✅ | Capture photo, send to Ophir via Signal DM with scene description. |
| 25 | [Intercom: voice to Signal](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/25) | #20, #22, #24 | done ✅ | "Tell Ophir I'm heading out" → Vector agent sends Signal DM. |
| 31 | [LiveKit WebRTC session](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/31) | #10, #32 | done ✅ | Two-way LiveKit: mic + camera + audio + video in/out. Triggered via "robot call me" or `/call/join-url` endpoint. |
| 34 | [Physical test framework](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/34) | #32, #23 | done ✅ | Signal-based test checkpoints for physical robot verification. |

---

### Phase 6: Advanced Features — MOSTLY COMPLETE

Nice-to-haves and new capabilities unique to Vector.

| # | Issue | Dependencies | Status | Description |
|---|-------|-------------|--------|-------------|
| 26 | [Visual SLAM](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/26) | #10, #7 | done ✅ | Camera-only SLAM. Dead reckoning + visual landmarks + IMU fusion. |
| 28 | [Multi-modal expressions](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/28) | #14, #4, #21 | done ✅ | Coordinated face display + LEDs + sound for robot emotions/status. |
| 40 | [Cube interaction](https://github.com/ShesekBean/nuc-vector-orchestrator/issues/40) | #6, #7, #36 | not started | Detect and pick up Vector's light cube. New capability. |

---

### Phase 7: Autonomous Behaviors — COMPLETE

Indoor navigation, exploration, and autonomous patrol behaviors.

| Feature | Status | Description |
|---------|--------|-------------|
| IMU fusion + dead reckoning | done ✅ | Fuse IMU data with wheel odometry for position tracking. Auto-save charger waypoint. |
| A* path planning | done ✅ | Grid-based pathfinding with waypoint management for indoor navigation. |
| Autonomous explorer | done ✅ | Explores rooms autonomously, Signal room naming, auto-charge when battery low. |
| Home Guardian (smart patrol) | done ✅ | Smart patrol & security system — scheduled patrols, person detection alerts, perimeter monitoring. |
| Drive off charger + override control | done ✅ | Explorer drives off charger on startup, override control on demand. |
| Dead reckoning + charger auto-save | done ✅ | Resume after charge, auto-save charger waypoint for return-to-base. |
| QuietMode + behavior mode API | done ✅ | Release SDK control, wire-pod cloud_intent, keepalive thread. Behavior mode switching. |

---

### Phase 8: OpenClaw Skills — IN PROGRESS

Extending Vector's capabilities through OpenClaw skill ecosystem.

| Feature | Status | Description |
|---------|--------|-------------|
| Companion behavior system | done ✅ | Presence tracking, OpenClaw personality integration, engagement-adaptive behavior. |
| Fitness skill (Strava, Withings, Oura) | done ✅ | Health and fitness data aggregation via OpenClaw skill. |
| Monarch Money skill | done ✅ | Financial data and balance queries via OpenClaw skill. |

---

## Dependency Graph

```
Phase 0 (infra) ✅                 Phase 1 (basic) ✅
┌──────────┐                       ┌───────────────┐
│ #1 wire  │──► #2 OSKR ──► #39 ──►│ #3 gRPC test  │
│    pod   │                       │   (GATE)       │
└──────────┘                       └───────┬───────┘
                                      ┌────┼────┬────┬────┐
#29 OpenVINO ✅                       │    │    │    │    │
#30 PGM auto-unstick ✅               ▼    ▼    ▼    ▼    ▼
#36 Event bus ✅                     #4   #5   #6   #7   #18
                                    LED  head  lift motor  mic
                                     │    │    │    │      │
                                     └──┬─┘    │    │      ▼
                                        │      │    │    #19 wake
                                        ▼      │    │      │
                                    #32 bridge◄┘    │      ▼
                                        │          │    #20 STT
                                        │          │      │
                              ┌─────────┼──────────┘      ▼
                              │         │              #21 TTS
                              │         ▼                  │
                              │    #10 camera              ▼
                              │      │    │            #22 routing
                              │      ▼    │                │
                              │    #11 YOLO                │
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
                                        │
                                        ▼
                              Phase 7: Navigation + Exploration
                              (IMU fusion, A*, explorer, Home Guardian)
                                        │
                                        ▼
                              Phase 8: OpenClaw Skills
                              (companion, fitness, monarch-money)
```

## Interaction Model

All interactions can be initiated two ways:

1. **Voice** — "Hey Vector, [command]" → Vector mic → Porcupine v4 wake word → wire-pod Vosk STT → Vector agent → action
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
| Patrol | "start patrol" | "robot patrol" | Phase 7 |
| Explore | "explore" | "robot explore" | Phase 7 |

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

**Mic streaming status:** Mic audio streaming is WORKING without a custom binary. The solution uses SDK `signal_power` PCM for wake word detection and wire-pod chipper tap for STT audio. The vector-streamer native binary (mic DGRAM proxy) was built as a backup approach but is not required for current operation.

| Feature | Status | Details |
|---------|--------|---------|
| **Mic audio streaming** | Working (no custom binary needed) | SDK `signal_power` PCM provides audio data for Porcupine wake word detection. Wire-pod chipper tap captures post-wake-word audio for Vosk STT. Two-process architecture avoids the ADSP exclusivity problem. |
| **vector-streamer** | Built (backup) | Native mic DGRAM proxy binary, cross-compiled for ARM32. Available as fallback if SDK PCM path proves insufficient. |
| **DisplayFaceImageRGB fix** | Applied (in libcozmo_engine.so) | Static buffer + stride conversion fix for Vector 2.0 (Xray) 160x80 OLED display. |

**Build environment:**
- Target: ARM (Qualcomm Snapdragon 212 / MSM8909, 32-bit ARMv7)
- Cross-compiler: `arm-linux-gnueabihf-gcc` or Qualcomm/Yocto SDK toolchain
- Vector OS: Yocto Linux (busybox userland)
- Key libraries on Vector: `/anki/lib/libaudio_engine.so` (2.8MB, ADSP mic access), `/anki/lib/libAudioPlayer.so`, `/anki/lib/libutil_audio.so`
- Deploy: SCP to Vector, run via SSH

---

## R3 Lessons Applied

Key lessons from R3 (nuc-orchestrator) baked into this plan:

1. **Standalone-first** — each subsystem has standalone test scripts (#33) before integration
2. **State machines** — follow behavior uses explicit states (IDLE → SEARCHING → FOLLOWING)
3. **PD control with EMA** — reactive control with exponential moving average smoothing for stable following
4. **Echo cancellation** — TTS→mic feedback loop prevention from day 1 (#37)
5. **Gain tuning** — PD controller gains tuned for Vector's floor-level camera (person fills frame at 1.5m)
6. **int16 audio** — Porcupine requires int16, NOT float32
7. **No over-engineering** — event bus is ~100 lines, not ROS2
8. **Process supervisor** — graceful startup/shutdown, not ad-hoc scripts (#35)
9. **Two-process voice architecture** — separate wake word and STT processes avoids resource contention
10. **Dead reckoning** — essential for navigation without LiDAR; IMU fusion compensates for wheel slip
11. **Auto-charge** — autonomous robots must manage their own battery; charger waypoint auto-saved on first dock
12. **Behavior modes** — QuietMode and behavior switching prevent SDK conflicts between autonomous and on-demand control
13. **Signal as test oracle** — send camera frames to Signal during physical tests for human verification

See `docs/vector/oskr-research.md` for full portability matrix.
