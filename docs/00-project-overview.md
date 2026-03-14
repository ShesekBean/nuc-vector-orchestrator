# Project Vector (Vector Edition) — Project Overview

**Last updated:** 2026-03-14
**Purpose:** Definitive onboarding document for anyone new to the project.

---

## What Is This?

Project Vector is a distributed multi-agent robotics system where a robot (Anki/DDL Vector 2.0 with OSKR unlock) codes itself, tests itself, and improves itself — with minimal human intervention.

Two machines coordinate via GitHub Issues in a single monorepo:
- **NUC "desk"** (Intel i7-1360P, 32GB RAM, Ubuntu, Iris Xe iGPU) — runs ALL inference, control, orchestration, and automation
- **Vector 2.0** (Qualcomm Snapdragon 212, OSKR unlocked) — thin gRPC client, hardware only

**Repo:** `ShesekBean/nuc-vector-orchestrator` (monorepo)
**Parent:** `ShesekBean/nuc-orchestrator` (R3 robot — reference architecture, archived)
**Human:** Ophir (communicates via Signal messenger, phone +14084758230)
**Started:** 2026-03-10 — rapid development over 5 days
**Scale:** ~188 commits, ~168 Python modules, 17 C source files, 4 OpenClaw skills

---

## System Architecture

```
Ophir (phone/laptop)
├── Signal app ────► OpenClaw gateway ─► robot-control skill ─► Bridge HTTP ─► Vector
├── LiveKit Cloud (robot-cam room) ◄──► LiveKit bridge ◄──► Vector camera/mic/speaker
└── Signal feedback ◄── PGM notifications, Intercom photos

NUC "desk" (THIS MACHINE — ALL COMPUTE)
├── Vector Supervisor (apps/vector/supervisor.py)
│   └── 16 ordered components (see Component Inventory below)
│
├── Bridge Server (localhost:8081, 172.17.0.1:8081 for Docker)
│   ├── HTTP REST → Vector SDK/gRPC translation (aiohttp)
│   └── Face enrollment API: /face/enroll, /face/list, /face/remove, /face/recognize
│
├── Agent Loop (nuc-agent-loop.service)
│   ├── Issue Workers (up to 4 parallel: 2 Vector + 2 NUC slots)
│   ├── PR Review Hook (Haiku — independent review on every PR)
│   ├── PGM (5-min health audits, auto-unstick, Signal notifications)
│   └── Coach (quality gate on Ophir instructions)
│
├── OpenClaw Gateway (Docker, port 18889, WebSocket)
│   ├── robot-control skill — Signal → bridge curl commands
│   ├── companion skill — presence signals → warm personality responses
│   ├── fitness skill — Strava/Withings/Oura tracking
│   └── monarch-money skill — financial queries (read-only)
│
├── wire-pod (port 8080/443, systemd root service)
│   └── Vector cloud replacement: auth, STT (Vosk), intent routing
│
├── Inference Pipeline (ALL on NUC, OpenVINO)
│   ├── YOLO11n person detection (~15fps, OpenVINO IR)
│   ├── KalmanTracker (position-only [cx,cy,vx,vy], frozen bbox, 10Hz)
│   ├── Face recognition (YuNet + SFace, enrollment via bridge API or standalone script)
│   ├── Scene description (camera + Claude Vision API)
│   ├── Wake word (Porcupine PV, two-process architecture)
│   ├── STT (wire-pod Vosk, routed to OpenClaw)
│   └── TTS (Vector built-in say_text() — no external TTS service)
│
├── Voice Proxy (scripts/openclaw-voice-proxy.py, default port 8095)
│   └── Bridges wire-pod STT → OpenClaw LLM agent
│
├── Intercom Server (scripts/intercom-server.py, default port 8095)
│   └── HTTP relay → Signal DM (photo + text to Ophir)
│   └── Note: shares default port with voice proxy — run one at a time or override PORT env var
│
├── LiveKit Bridge (apps/vector/src/livekit_bridge.py)
│   └── WebRTC: camera out + mic out (Opus) + audio in (speaker) + video in (OLED)
│
├── Docker: OpenClaw gateway (port 18889) [EXISTING — DO NOT TOUCH]
└── Docker: openclaw-dns (dnsmasq, 172.20.0.53) [EXISTING — DO NOT TOUCH]

Vector 2.0 (ROBOT — THIN CLIENT ONLY)
├── 800×600 camera (OV7251, ~120° FOV)
├── 4-mic beamforming array (ADSP-based, 15625 Hz native)
├── 160×80 OLED display (SDK sends 184×96, vic-engine converts stride)
├── Differential drive (tank treads, max ~200mm/s)
├── Lift mechanism (motorized forklift)
├── Touch sensor (capacitive, head)
├── Cliff sensors (4x, safety-critical)
├── vector-streamer native binary (mic DGRAM proxy + Opus encode + TCP:5555 to NUC)
├── wire-pod client (replaces Anki cloud)
└── Firmware: WireOS 3.0.1.32oskr (slot B), stock 2.0.1.6091oskr (slot A fallback)
```

### Communication Paths

```
Vector ──gRPC (WiFi)──────────────► NUC (SDK: wirepod-vector-sdk 0.8.1, import anki_vector)
Vector ──TCP:5555 (Opus audio)────► NUC (vector-streamer → MicChannel → LiveKit)
NUC ───gRPC motor/speaker cmds───► Vector
NUC ───HTTP bridge:8081──────────► OpenClaw Docker (robot-control skill curls bridge)
NUC ───WebSocket:18889───────────► OpenClaw gateway (companion skill, voice proxy)
NUC ───Signal (via OpenClaw)─────► Ophir's phone (PGM notifications, intercom photos)
NUC ───LiveKit Cloud (WebRTC)────► Ophir's browser/phone (robot-cam room)
```

---

## Component Inventory

The Vector Supervisor (`apps/vector/supervisor.py`) manages 16 components with ordered startup (reverse-order shutdown). Orders 1-2 are connection + event bus (handled before components):

| Order | Component | Class | Module | Critical | Needs Connection |
|-------|-----------|-------|--------|----------|-----------------|
| 3 | `motor_controller` | `MotorController` | `src/motor_controller.py` | Yes | Yes |
| 4 | `sensor_handler` | `SensorHandler` | `src/sensor_handler.py` | Yes | Yes |
| 5 | `camera_client` | `CameraClient` | `src/camera/camera_client.py` | No | Yes |
| 6 | `led_controller` | `LedController` | `src/led_controller.py` | No | Yes |
| 7 | `lift_controller` | `LiftController` | `src/lift_controller.py` | No | Yes |
| 8 | `display_controller` | `DisplayController` | `src/display_controller.py` | No | Yes |
| 9 | `person_detector` | `PersonDetector` | `src/detector/person_detector.py` | No | No |
| 10 | `audio_client` | `AudioClient` | `src/voice/audio_client.py` | No | Yes |
| 11 | `wake_word` | `WakeWordDetector` | `src/voice/wake_word.py` | No | No |
| 12 | `speech_output` | `SpeechOutput` | `src/voice/speech_output.py` | No | Yes |
| 13 | `echo_suppressor` | `EchoSuppressor` | `src/voice/echo_cancel.py` | No | No |
| 14 | `voice_bridge` | `OpenClawVoiceBridge` | `src/voice/openclaw_voice_bridge.py` | No | Yes |
| 15 | `follow_planner` | `FollowPlanner` | `src/planner/follow_planner.py` | No | Yes |
| 16 | `companion` | `CompanionSystem` | `src/companion/__init__.py` | No | No |

The supervisor also provides:
- **WiFi disconnect detection** via SDK `connection_lost` event → automatic reconnect with exponential backoff
- **Component crash detection** via health monitor thread (10s interval) → auto-restart
- **Battery-aware reduction** — low battery stops follow_planner, person_detector, voice_bridge; sets LED indicator
- **sd_notify READY=1** for systemd integration
- **Graceful SIGTERM shutdown** — reverse component order, stow lift, center head

---

## Voice Pipeline

```
Vector mic → Porcupine PV wake word → wire-pod (Vosk STT)
  → IntentGraph → openclaw-voice-proxy.py → OpenClaw LLM → Vector say_text()
```

**Key details:**
- **wire-pod** (`wire-pod.service`, root): Replaces Anki cloud. Handles wake word detection, STT (Vosk), and intent routing.
- **Voice proxy** (`scripts/openclaw-voice-proxy.py`): Standalone script bridging wire-pod to OpenClaw via OpenAI-compatible API. Serializes requests to prevent double-trigger abort. 60s timeout for tool-heavy queries.
- **Built-in intents disabled**: All wire-pod intents set to `requiresexact=True` in `en-US.json` — conversational queries route to OpenClaw, not intercepted by built-in handlers.
- **Sit-still default**: Vector sits still via firmware config — `highLevelAI.json` stripped to Wait-only (1 state, 0 transitions), `quietMode.json` set to 24h active time. Bridge connects with `behavior_control_level=None` (no SDK behavior control). Wake word, voice commands, and SDK commands all still work (they interrupt Wait at higher priority in the behavior tree). Playful mode: `POST /mode {"mode":"playful"}` grants override control for up to 8 min, then auto-reverts to quiet.
- **Voice context prefix**: Prepends context to every message telling OpenClaw the speaker is Ophir.
- **Dual-path routing**: wire-pod intents → bridge HTTP for hardware commands; conversational queries → OpenClaw agent.
- **Echo cancellation** (`EchoSuppressor`): Pauses mic during `say_text()` output + holdoff period to prevent feedback loops.
- **Audio path**: `AudioClient` receives PCM from SDK `AudioFeed` (decoded from `signal_power` field, int16 LE at 15625 Hz), resamples to 16000 Hz, stores in thread-safe ring buffer for wake word and STT.
- **"Tell Ophir" relay**: Regex intercept in voice command router catches "tell Ophir [message]" → sends via Signal intercom.

---

## Vision Pipeline

```
CameraClient (800×600 BGR, ring buffer, 15fps)
  → PersonDetector (YOLO11n OpenVINO IR, multi-stage low-light preprocessing)
    → KalmanTracker (position-only [cx,cy,vx,vy], frozen bbox, IoU assignment, 10Hz)
      → TrackedPersonEvent → FollowPlanner + HeadTracker
  → FaceRecognizer (YuNet detection + SFace embeddings)
    → FaceRecognizedEvent → CompanionSystem + HomeGuardian
  → SceneDescriber (camera frame + YOLO boxes + Claude Vision API)
```

**Components:**
- **CameraClient** (`src/camera/camera_client.py`): 800x600 BGR frames from Vector gRPC `CameraFeed`. Ring buffer, polling fallback for SDK quirks. Configurable FPS.
- **PersonDetector** (`src/detector/person_detector.py`): YOLO11n nano model in OpenVINO IR format. COCO class 0 (person). ~47ms inference on NUC (vs 97ms for YOLO11s). Adaptive confidence threshold. Multi-stage low-light preprocessing for Vector's inherently dark camera.
- **KalmanTracker** (`src/detector/kalman_tracker.py`): State vector `[cx, cy, vx, vy]`. Bbox dimensions frozen at last measurement (not predicted — R3 lesson: predicting bbox size caused wild oscillation). IoU-based assignment. 10Hz prediction rate.
- **FaceRecognizer** (`src/face_recognition/face_recognizer.py`): YuNet face detection + SFace embedding extraction (ONNX models). Cosine similarity matching, 0.363 threshold. JSON enrollment database on disk (`apps/vector/data/face_database.json`). Enrollment via standalone script (`test_enroll_face.py`) or bridge API (`POST /face/enroll`). Body reference crops stored in `apps/vector/data/reference_images/<name>/`.
- **SceneDescriber** (`src/camera/scene_describer.py`): Sends camera frame + YOLO detections to Claude Vision API for natural language scene description.

---

## Person Following

**State machine:** `IDLE` → `SEARCHING` → `FOLLOWING` → `SEARCHING` → ...

**Key design:** TURN FIRST, THEN DRIVE. Never mix turning and driving on differential drive (mixing causes arcs and circles).

**P controllers:**
- Turn (horizontal centering): `kp_turn=0.2`, dead zone ±15% frame width
- Drive (distance via bbox height): `kp_drive=2.5`, target bbox height 70% frame, dead zone 3% frame
- Head pitch (vertical tracking): `kp_head=0.10`
- Max wheel speed: 140mm/s (conservative, Vector max ~200mm/s)

**Search behavior:** Head sweep (look around) → velocity-biased turn → body scan → 2 cycles max → IDLE.

**HeadTracker** (`src/planner/head_tracker.py`): Independent P-controller for head pitch, runs alongside follow planner.

---

## Navigation & Exploration

### VisualSLAM (`src/planner/visual_slam.py`)
Camera-only SLAM replacing R3's LiDAR SLAM:
- ORB feature detection + motor dead reckoning for absolute scale
- Occupancy grid: 5m x 5m, 50mm cell resolution (FREE / OCCUPIED / UNKNOWN)
- Visual landmark map for loop closure detection
- Monocular camera cannot determine scale alone — distance from motor odometry, rotation correction from visual features

### NavController (`src/planner/nav_controller.py`)
High-level navigation state machine: `IDLE` → `PLANNING` → `NAVIGATING`
- A* path planning on occupancy grid
- Turn-then-drive path segments
- Obstacle replanning (3 attempts before giving up)
- Passive mapping: SLAM continues building grid during all movement

### AutonomousExplorer (`src/planner/exploration.py`)
Frontier-based room exploration:
- Drives toward nearest boundary between FREE and UNKNOWN cells
- On new area detection: stops, takes photo, sends Signal message asking Ophir to name the room
- Reads reply from `signal-inbox.jsonl`, saves position as named waypoint
- Auto-charge: drives to charger on low battery, resumes after charge
- Dead reckoning integration for scale estimation

### HomeGuardian (`src/planner/patrol.py`)
Smart patrol and security system with two modes:
- **Patrol mode**: Cycles through saved waypoints, scanning at each
- **Sentry mode**: Parks at one spot, continuously monitors

At each waypoint:
1. Head sweep (look around)
2. YOLO person detection + Kalman tracking
3. Person found → face recognition (known vs unknown)
4. Unknown person → Signal alert with photo + scene description (Claude Vision)
5. Known person → log (configurable alert)
6. Voice narration: "Checking the kitchen... all clear!"
7. Activity log accessible via bridge API

---

## Companion Behavior System

A proactive personality layer that makes Vector feel alive and responsive to people.

### PresenceTracker (`src/companion/presence_tracker.py`)
State machine driven by face recognition, person detection, touch, and wake word events:
- Tracks arrival, departure, still_present (check-in) states
- Engagement score: 0.0-1.0, decays at 0.0005/s (~0.03/min, drops 1.0→0 in ~33 min)
- Boosted by interactions (touch, wake word, face recognition)
- Absence timeout: 120s without detection → departure
- State persisted to `~/.openclaw/workspace/memory/companion-state.json`

### CompanionDispatcher (`src/companion/dispatcher.py`)
Engagement-adaptive throttling + quiet hours:
- Check-in intervals: high engagement = 20 min, medium = 45 min, low = 90 min
- Greeting minimum interval: 120s
- Touch response minimum interval: 30s
- Departure only signaled if session > 5 min
- Quiet hours: 23:00-07:00 (fallback if no Oura sleep data)
- Battery alerts at <20%

### OpenClaw Companion Skill (`apps/openclaw/skills/companion/SKILL.md`)
Warm personality responses to presence signals:
- Varied greetings, check-ins, goodbyes
- Physical expression via bridge API (LED colors, head movement, sounds)
- Multi-person awareness: Ophir, Smadara, unknown (sends camera frame for identification)

---

## Signal & OpenClaw Integration

### OpenClaw Gateway
- Docker container on port 18889 (WebSocket protocol)
- Signal-cli daemon → monitor.ts SSE → gateway → agent → send.ts JSON-RPC
- Config: `~/.openclaw/openclaw.json`

### 4 Skills
All skills are hot-deployable directories under `~/.openclaw/workspace/skills/<name>/` with YAML frontmatter `SKILL.md`:

1. **robot-control** (`apps/openclaw/skills/robot-control/SKILL.md`) — Signal text commands → bridge HTTP → Vector actions (move, look, speak, photo, follow, patrol, explore)
2. **companion** (`apps/openclaw/skills/companion/SKILL.md`) — Presence signals → personality responses with physical expression
3. **fitness** (`apps/openclaw/skills/fitness/SKILL.md`) — Strava, Withings, Oura Ring data tracking and reporting
4. **monarch-money** (`apps/openclaw/skills/monarch-money/SKILL.md`) — Financial queries via Monarch Money API (read-only)

### Intercom (`src/intercom.py` + `scripts/intercom-server.py`)
- `Intercom` class sends text + JPEG photos to Ophir via HTTP POST to intercom-server
- Intercom server relays to Signal DM via OpenClaw gateway
- Used by: HomeGuardian alerts, exploration room naming, scene descriptions

### Voice-to-Signal Relay
- "Tell Ophir [message]" detected by voice command router → regex intercept → Signal send via intercom

---

## Agent Loop & Automation

### Agent Loop (`apps/control_plane/agent_loop/`)
Entry point: `python3 -m apps.control_plane.agent_loop` (systemd: `nuc-agent-loop.service`)

Main loop:
1. `git pull` latest code
2. Check `gh issue list` for `assigned:worker` open issues
3. Dispatch up to 4 workers in parallel (each in its own git worktree at `/tmp/{vector|nuc}-worker-issue-{num}`)
   - 2 Vector slots for `component:vector` issues (get gRPC context)
   - 2 NUC slots for NUC-only issues
4. Each worker handles full lifecycle: design → code → self-review → test → merge
5. If no issues found → sleep 1 minute → check again
6. PGM health check every 5 minutes (separate from poll cycle)
7. On failure → retry next cycle, PGM flags if stuck

### Account Rotation (`agent_loop/account_rotation.py`)
- 3 API accounts, 2 hits per account, 5-min cooldown
- Automatic rotation on quota detection

### PGM (`agent_loop/pgm.py`)
Issue health auditor + Ophir notifier (Haiku model):
- Audits open issues for CI health, PR status, stuck detection
- Auto-unstick: parses `## Dependencies` sections for `#N` references; when all dependencies close, removes `stuck` label, adds `assigned:worker`
- Signal notifications with `📊 PGM:` prefix via `scripts/pgm-signal-gate.sh`
- Rate-limited notifications to avoid spam

### Coach
Quality gate on Ophir's instructions (Opus model):
- Runs BEFORE worker dispatch on ALL instructions from Ophir
- If concerns → Signal Ophir with `🏋️ COACH:` prefix, STOP, wait for reply
- If no concerns → stays silent, orchestrator creates issue

### PR Review Hook (Haiku)
Independent reviewer on every PR diff:
- Fresh eyes — no context about design rationale
- Security check: catches `.claude/CLAUDE.md` modifications
- Fires automatically after each worker invocation

### Labels
- `assigned:worker` — ready for worker dispatch
- `component:vector` — Vector-specific code (worker gets gRPC context)
- `blocker:needs-human` — needs physical test or human decision
- `stuck` — issue needs investigation (auto-unstick when dependencies resolve)
- `milestone` — something notable is working (notify Ophir)

---

## LiveKit Video Bridge

**Module:** `apps/vector/src/livekit_bridge.py`
**Cloud URL:** `wss://robot-a1hmnzgn.livekit.cloud`, Room: `robot-cam`

Two-way streaming:
- **Camera OUT**: Vector camera → CameraClient → JPEG → RGBA VideoFrame → LiveKit (15fps)
- **Mic OUT**: vector-streamer DGRAM proxy → Opus encode → TCP:5555 → MicChannel → PCM → LiveKit
- **Audio IN**: LiveKit remote audio → PCM → Vector `stream_wav_file()`
- **Video IN**: LiveKit remote video → downscale 160x80 → `DisplayFaceImageRGB` → Vector OLED

Auto-disconnect: 30s initial wait, 3s after last participant leaves.

---

## On-Demand Media Layer

**Module:** `apps/vector/src/media/`
**API:** `POST /media/channels`

Four independently controllable media channels:

| Channel | Direction | Description |
|---------|-----------|-------------|
| `camera` (video_in) | Vector → NUC | JPEG frames via CameraChannel fan-out (subscribers get frames via queue) |
| `mic` (audio_in) | Vector → NUC | PCM from vector-streamer Opus → MicChannel |
| `speaker` (audio_out) | NUC → Vector | TTS via say_text() or PCM via stream_wav_file() |
| `display` (video_out) | NUC → Vector | PIL images → 160x80 OLED (handles SDK 184x96 stride conversion) |

Channels are lazy — only consume resources when started. Multiple services can subscribe to the same input channel. MediaService is created by ConnectionManager and used by LiveKit bridge.

Unified endpoint: `POST /media/channels {"action": "start|stop", "video_in": true, "audio_in": true, ...}`

---

## Native Binaries (Cross-compiled for Vector ARM32)

Source: `apps/vector/native/`
Build environment: Jetson (192.168.1.70), vicos-sdk 5.3.0-r07 Clang toolchain.

### vector-streamer (`native/vector-streamer/`)
7 C source files:
- `main.c` — entry point, TCP server, Opus encode
- `mic.c/h` — server-side DGRAM proxy for `mic_sock_cp_mic` (intercepts mic audio from vic-anim)
- `engine_proxy.c/h` — engine-anim DGRAM proxy (deferred ANKICONN: waits for vic-engine handshake before connecting vic-anim)
- `tcp_server.c/h` — framed TCP protocol (port 5555)
- `opus_encoder.c/h` — Opus encoding wrapper
- `protocol.h` — frame types: OPUS, H264, JPEG, PCM, CMD

Systemd service on Vector: `vector-streamer.service` (Before=vic-engine, After=vic-anim)

### DisplayFaceImageRGB Patch
Patch to `libcozmo_engine.so` on Vector:
- Static buffer + stride conversion (184 → 160 columns) for Vector 2.0 (Xray) display
- Fixes SDK assumption of 184x96 → actual 160x80 hardware

### Firmware Patches (ShesekBean/wire-os-victor fork)

Source: `/tmp/victor-build/victor/` on Jetson (192.168.1.70)
Branch: `vector-v4z4-patches`
Build: Docker cross-compile (`./build/build-v.sh`)

Two patches deployed to Vector:

1. **DisplayFaceImageRGB stride fix** (`engine/components/animationComponent.cpp`) — Static buffer + stride 184→160 conversion for Xray display. Deployed as `/anki/lib/libcozmo_engine.so`.
2. **HighLevelAI Wait-only** (`highLevelAI.json`) — Stripped to single Waiting state with zero transitions. Vector never leaves Wait at the HighLevelAI level. Deployed to `/anki/data/assets/cozmo_resources/config/`. Source in `deploy/vector/behavior-configs/`.
3. **QuietMode 24h** (`quietMode.json`) — `activeTime_s: 86400` as belt-and-suspenders backup. Same deploy path.

---

## Hardware Notes

| Component | SDK Docs Say | Actually Is |
|-----------|-------------|-------------|
| Camera resolution | 640x360 | **800x600** RGB via gRPC CameraFeed |
| Display | 184x96 OLED | **160x80** OLED (SDK sends 184x96, vic-engine converts stride) |
| Mic interface | ALSA | **ADSP-based** (libaudio_engine.so FastRPC), SDK AudioFeed returns PCM in `signal_power` field |
| Mic sample rate | — | **15625 Hz** native, resampled to 16000 Hz on NUC |
| Firmware | — | WireOS 3.0.1.32oskr (slot B), stock 2.0.1.6091oskr (slot A fallback) |
| Drive model | — | Differential (tank treads), max ~200mm/s, no strafing |

---

## Key Differences from R3 (nuc-orchestrator)

| Aspect | R3 (nuc-orchestrator) | Vector (this repo) |
|--------|----------------------|-------------------|
| Robot compute | Jetson Orin Nano (GPU, CUDA) | NUC does everything (OpenVINO) |
| Communication | SSH + HTTP bridge + ROS2 | gRPC over WiFi |
| Containers | Docker on Jetson | No Docker on Vector |
| Framework | ROS2 Humble | Python + gRPC |
| Drive | Mecanum (omnidirectional) | Differential (tank treads, turn-then-drive) |
| LiDAR | RPLIDAR A1 | None (camera-only SLAM) |
| Cloud | None (self-hosted) | wire-pod (replaces Anki cloud) |
| Inference | CUDA (Jetson GPU) | OpenVINO (Intel iGPU + CPU) |
| Camera | USB webcam | Built-in 800x600 OV7251 |
| Mic | USB mic | Built-in 4-mic beamforming array |
| Display | None | 160x80 OLED |
| Native code | None | Cross-compiled ARM32 binaries |

---

## Testing

### Golden Test Suite (`tests/golden/`)
13 phase-gated test files, run with `pytest tests/golden/ -v`:

| Phase | File | Tests |
|-------|------|-------|
| 0 | `test_phase0_unit.py` | Unit tests (no hardware) |
| 1 | `test_phase1_preflight.py` | Connectivity, wire-pod |
| 2 | `test_phase2_hardware.py` | Camera, sensors, battery |
| 3 | `test_phase3_inference.py` | YOLO, face recognition |
| 4 | `test_phase4_voice.py` | Wake word, STT, TTS |
| 5 | `test_phase5_movement.py` | Motors, head, lift |
| 6 | `test_phase6_following.py` | Person-following pipeline |
| 7 | `test_phase7_signal.py` | Signal → OpenClaw → robot E2E |
| 8 | `test_phase8_agentloop.py` | Agent loop health |
| 9 | `test_phase9_eventbus.py` | Event bus integration |
| 10 | `test_phase10_services.py` | Systemd service health |
| 12 | `test_phase12_signal_robot.py` | Signal → robot E2E integration |
| 13 | `test_phase13_livekit.py` | LiveKit video bridge |

### Standalone Test Scripts (`apps/vector/tests/standalone/`)
12 self-contained subsystem tests (run individually): camera, motors, head, lift, LEDs, display, audio, mic, sensors, detection, follow, and more.

### Unit Tests (`tests/vector/`)
Pytest-based tests with full mock coverage (no hardware required).

### Physical Test Framework
Signal checkpoint flow: send artifacts (camera frames, screenshots) to Signal for Ophir to visually confirm pass/fail.

---

## Process Management

```bash
# Start all services
bash scripts/start-all.sh

# Stop all services
bash scripts/kill-all.sh

# Manual service control
systemctl --user stop nuc-agent-loop.service
systemctl --user start nuc-agent-loop.service
```

### Systemd Services
- `nuc-agent-loop.service` — agent loop (user service)
- `vector-supervisor.service` — component supervisor (user service)
- `vector-bridge.service` — HTTP-to-gRPC bridge (user service)
- `wire-pod.service` — wire-pod cloud replacement (root service)
- `fitness-token-refresh.service` — Strava/Withings token refresh (user service)
- `vector-streamer.service` — native mic proxy on Vector (root service on Vector)

---

## Key Files to Read First

1. **`REPO_MAP.md`** — full directory structure with every module listed
2. **`.claude/CLAUDE.md`** — architecture, agent definitions, safety rules (IMMUTABLE)
3. **`docs/vector/oskr-research.md`** — OSKR SDK research and feature portability from R3
4. **`docs/ROADMAP.md`** — issue tracking and project milestones
5. **`docs/vector/setup-guide.md`** — hardware setup and configuration
