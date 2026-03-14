# Vector 2.0 OSKR — Platform Research & Feature Portability

**Date:** 2026-03-10 (initial research), updated 2026-03-14 (actual results)
**Source:** SDK analysis of [wire-pod](https://github.com/kercre123/wire-pod) and [vector-go-sdk](https://github.com/digital-dream-labs/vector-go-sdk), plus actual implementation experience

---

## Platform Overview

| Spec | Yahboom ROSMASTER R3 (current) | Anki/DDL Vector 2.0 OSKR |
|------|-------------------------------|--------------------------|
| CPU | Jetson Orin Nano Super (6-core A78AE + GPU) | Qualcomm Snapdragon 212 (4-core A7, 1.3GHz) |
| RAM | 8GB | 1GB |
| Camera | IMX219 CSI, 160° fisheye, 1280x720 | OV7251, ~120° FOV, 800x600 |
| Mic | USB JMTek (external) | 4-mic array (built-in, beamforming) |
| Speaker | USB JMTek (external) | Built-in speaker |
| LiDAR | RPLIDAR A1 (360° scan) | None (4 cliff sensors + ToF) |
| IMU | ICM chip via MCU | Built-in (accelerometer + gyro) |
| Drive | Mecanum wheels (omnidirectional) | Differential drive (tank treads) |
| Connectivity | Ethernet + WiFi | WiFi only |
| OS | JetPack 6.2.1 (Ubuntu-based) | Yocto Linux (custom) |
| SDK | ROS2 Humble + Python | gRPC + Go/Python |
| Size | ~30cm wide, ~15cm tall | ~10cm × 6cm × 7cm |

## OSKR Access Model

- **OSKR unlock** gives root SSH access to Vector's Linux OS
- **wire-pod** replaces Anki/DDL cloud servers entirely (runs on NUC)
- Communication: **gRPC over WiFi** (Vector ↔ NUC)
- All heavy compute runs on NUC — Vector's Snapdragon 212 is too weak for inference

## Feature Portability Matrix

### Fully Portable (minimal changes) — ACTUAL RESULTS

| Feature | Current (R3) | Vector Implementation | Actual Result |
|---------|-------------|-------------|-------|
| Person detection (YOLO) | Jetson GPU, ~3Hz | YOLO11n on OpenVINO, ~15fps, 47ms/frame | **Faster than R3** — no Jetson GPU contention, OpenVINO well-optimized on NUC i7 |
| Face recognition | YuNet + SFace on Jetson | YuNet + SFace ONNX on NUC | **Works great** — more compute headroom than Jetson |
| Voice pipeline | Wake word → STT → Command → TTS | Porcupine PV wake word → wire-pod Vosk STT → openclaw-voice-proxy → OpenClaw → say_text() | **Working** — Porcupine (not openwakeword), wire-pod Vosk for STT (not OpenClaw Talk Mode directly), openclaw-voice-proxy bridges the gap |
| LED control | Rosmaster_Lib API | `set_eye_color(hue, saturation)` | **Working** — backpack LEDs are NOT controllable via SDK; eye color is the primary visual indicator |
| Text-to-speech | Kokoro/Piper on Jetson | say_text() (Vector built-in TTS) | **Works perfectly** — onboard, zero cost, no audio streaming needed |
| Signal integration | OpenClaw → bridge HTTP | OpenClaw → gRPC | Same architecture, working |
| Intercom (text/photo) | Bridge → NUC HTTP → Signal | gRPC → NUC → Signal | Simpler — no Jetson bridge needed |
| Agent loop / workers | GitHub Issues dispatch | Identical | No changes needed |

### Portable with Rearchitecting — ACTUAL RESULTS

| Feature | Current (R3) | Vector Implementation | Actual Result |
|---------|-------------|-----------------|------------|
| Person following | PD controller, ~2Hz YOLO | P controller with turn-first-then-drive, ~2Hz loop | **Working** — WiFi latency acceptable, Vector's slow speed (~200mm/s) makes 2Hz responsive enough |
| Servo tracking | Direct PWM, 50Hz updates | Head tracking at ~10Hz with P-controller + slew limiting | **Working** — slew limiting smooths movement, 10Hz is sufficient |
| Movement control | Mecanum (omnidirectional strafe) | Turn-then-drive planner with cliff-safe differential drive | **Working well** — turn-then-drive pattern works naturally for Vector |
| Camera streaming | CSI direct, 720p | gRPC `CameraFeed`, 800x600 at ~15fps | **Working** — 800x600 (not 640x360 as initially expected), sufficient for YOLO |
| Scene description | YOLO on Jetson GPU | YOLO on NUC + Claude Vision API | Frame transfer over WiFi, works fine |

### Not Portable (missing hardware) — ACTUAL ALTERNATIVES IMPLEMENTED

| Feature | Current (R3) | Initial Concern | Actual Implementation |
|---------|-------------|-------------------|-------------|
| SLAM / Nav2 | RPLIDAR A1 360° scan | No LiDAR | **Visual SLAM with ORB features + motor dead reckoning** — implemented and working, builds occupancy grid from camera |
| Waypoint navigation | Nav2 + SLAM map | No LiDAR for costmap | **A* path planning on occupancy grid** — working, uses visual SLAM map |
| Obstacle avoidance | LiDAR forward cone | ToF + cliff sensors only | **Camera-based using YOLO bbox area ratio as distance proxy** — working, combined with cliff sensors for safety |
| IMU gyro gimbal | Direct IMU read, 50Hz | ~~No exposed IMU API~~ | **IMU IS available** via `robot.status` (accelerometer + gyro). Used for dead reckoning fusion. Initial research was wrong about this. |
| Battery monitoring | MCU voltage read | gRPC `BatteryState` | **Working** — gRPC BatteryState + LiPo voltage curve for percentage estimation |

### New Capabilities (Vector has, R3 doesn't) — IMPLEMENTATION STATUS

| Feature | Description | Status |
|---------|-------------|--------|
| Face display | **160x80 OLED** (NOT 184x96). SDK sends 184x96 images, patched `libcozmo_engine.so` does stride conversion on-robot. Used for face animations, status display, expressions via expression_engine.py | **Working** — required firmware patch |
| Lift mechanism | Motorized forklift with named presets (low/carry/high), auto-stow on shutdown | **Working** |
| Cube interaction | Detects and interacts with light cube (tap, roll, stack) | Not yet implemented |
| Cliff detection | 4 cliff sensors, bitmask-based safety gate integrated into motor_controller.py | **Working** — safety-critical, prevents falls |
| Capacitive touch | Top-of-head touch sensor, emits events via sensor_handler.py | **Working** |
| Built-in beamforming | 4-mic array, ADSP-based (NOT ALSA — ALSA doesn't work on Vector). Accessed via SDK AudioFeed `signal_power` field (int16 PCM at 15625Hz, resampled to 16000Hz on NUC) | **Working** — required research to find viable audio path |

## Architecture: Vector Orchestrator (Actual)

```
Ophir (laptop/phone)
├── Signal app → texts Vector (via OpenClaw)
├── Phone on tripod → LiveKit Cloud room (robot-cam, for vision oracle tests)
└── Signal feedback (from PGM, Orchestrator, Coach)

NUC "desk" (THIS MACHINE — ALL COMPUTE)
├── Orchestrator (interactive session — talks to Ophir via Signal)
│   └── Coach — quality gate on ALL Ophir instructions
├── Agent Loop (nuc-agent-loop.service — dispatches workers)
│   ├── Issue Worker(s) — up to 4 parallel (2 Vector + 2 NUC slots)
│   ├── PR Review Hook — independent Haiku review on every PR
│   ├── PGM — issue health auditor + Ophir notifier (every 5 min)
│   └── Vision Agent — phone camera test oracle (LiveKit)
├── wire-pod (Vector cloud replacement — auth, Vosk STT, intent engine)
├── Supervisor (apps.vector — component lifecycle: startup, health, reconnect)
├── Inference Pipeline (ALL on NUC, OpenVINO)
│   ├── YOLO11n person detection (~15fps, 47ms/frame, OpenVINO IR)
│   │   └── Kalman filter tracker (smooths detections at 10Hz)
│   ├── Face recognition (YuNet + SFace ONNX)
│   ├── Scene description (camera + Claude Vision API)
│   ├── Wake word detection (Porcupine PV)
│   ├── STT (wire-pod Vosk → openclaw-voice-proxy → OpenClaw)
│   └── TTS (Vector built-in say_text() — zero cost)
├── Navigation
│   ├── Visual SLAM (ORB features + motor dead reckoning + IMU fusion)
│   ├── A* path planning on occupancy grid
│   └── Camera-based obstacle detection (YOLO bbox area ratio)
├── Companion System (presence tracker → OpenClaw personality → expressions)
├── Media Layer (4 on-demand channels: camera fan-out, mic, speaker, display)
├── Planner (P controller → turn-then-drive → gRPC motor commands)
├── LiveKit Bridge (camera + mic out to LiveKit Cloud room)
├── Intercom Server (NUC HTTP → Signal DM for robot messages/photos)
├── Docker: OpenClaw (Signal gateway) [EXISTING — DO NOT TOUCH]
├── Docker: openclaw-dns (dnsmasq) [EXISTING — DO NOT TOUCH]
└── GitHub repo: ShesekBean/nuc-vector-orchestrator

Vector 2.0 (ROBOT — THIN CLIENT ONLY)
├── OSKR unlocked Linux (root SSH access)
├── wire-pod client (replaces Anki cloud)
├── gRPC server (camera 800x600, motors, mic, speaker, display, lift)
├── Patched libcozmo_engine.so (184x96 → 160x80 display stride fix)
├── vector-streamer (native C: mic DGRAM proxy + engine proxy + TCP bridge)
└── NO inference, NO ROS2, NO Docker, NO heavy processing
```

### Key Architecture Differences from R3

1. **No Docker on Vector** — too resource-constrained. All code runs on NUC.
2. **No ROS2** — Vector SDK events + lightweight NUC event bus replace ROS2 topics.
3. **No bridge.py on Vector** — HTTP→gRPC bridge runs on NUC (`apps/vector/bridge/`).
4. **All inference on NUC** — OpenVINO (no CUDA) for YOLO, face recognition, STT. Camera frames stream to NUC, commands stream back.
5. **wire-pod on NUC** — handles Vector's cloud dependencies (auth, intent engine, TTS).
6. **Hybrid event architecture** — Vector SDK provides 23 built-in events (face, object, sensors, wake word, connection state). NUC event bus adds custom events (YOLO detections, follow state, voice commands).

### Communication Flow

```
Vector ──gRPC (WiFi)──► NUC
                         ├── Camera frames (800x600 ~15fps) → YOLO → Kalman tracker → detection
                         ├── Mic audio (15625Hz PCM via AudioFeed) → Porcupine wake word → wire-pod Vosk STT → openclaw-voice-proxy → OpenClaw → command
                         ├── Planner → motor commands → gRPC → Vector
                         ├── say_text() → Vector speaker (built-in TTS)
                         └── LiveKit bridge → WebRTC → LiveKit Cloud room (robot-cam)

Signal ──OpenClaw──► NUC ──gRPC──► Vector
```

## Latency Analysis (Actual Measured)

| Pipeline | R3 (local) | Vector (WiFi) | Actual? |
|----------|-----------|---------------|-------------|
| Camera → YOLO → detection | ~300ms | **~47ms** (YOLO11n OpenVINO) + ~20ms WiFi transfer | **Much faster than R3** — OpenVINO on NUC i7 outperforms Jetson |
| Detection → motor command | <10ms | ~50ms | Yes |
| Full tracking loop | ~350ms | **~120ms** (~2Hz follow loop, head tracking ~10Hz) | **Better than expected** |
| Wake word → STT → command | ~2.5s | ~2.5s (same) | Yes — Porcupine + Vosk both run on NUC |
| TTS → speaker | ~1s | ~1.2s | Yes — say_text() is onboard |
| Camera → photo → Signal | ~3s | ~3.5s | Yes |

**Conclusion:** WiFi latency adds ~50-100ms per hop but YOLO inference is dramatically faster on NUC (47ms vs ~300ms on Jetson). Net result is the tracking loop is faster than R3. Head tracking runs at ~10Hz with P-controller + slew limiting. Voice pipeline uses wire-pod Vosk for STT rather than direct cloud API, keeping latency low.

## gRPC SDK Quick Reference (Python — wirepod-vector-sdk 0.8.1)

```python
# Camera feed — 800x600 JPEG frames at ~15fps
robot.camera.capture_single_image()  # or use camera_client.py streaming

# Motors — cliff-safe via motor_controller.py
robot.motors.set_wheel_motors(left_speed, right_speed)  # mm/s
robot.behavior.drive_straight(distance_mm, speed_mmps)
robot.behavior.turn_in_place(angle_deg, speed_dps)

# Head & Lift
robot.behavior.set_head_angle(angle_rad)  # -0.38 to 0.78 rad (~-22° to 45°)
robot.behavior.set_lift_height(height)    # 0.0 to 1.0 (presets: low/carry/high)

# LEDs — eye color only (backpack LEDs NOT controllable via SDK)
robot.behavior.set_eye_color(hue, saturation)  # hue 0.0-1.0, saturation 0.0-1.0

# Audio
robot.behavior.say_text("hello")  # built-in TTS, zero cost

# Display — 160x80 OLED (SDK accepts 184x96, patched libcozmo_engine.so converts stride)
robot.screen.set_screen_image(image_data)  # DisplayFaceImageRGB

# Sensors — includes IMU (accelerometer + gyro)!
robot.status  # accel, gyro, cliff sensors (bitmask), touch
robot.get_battery_state()  # voltage, charging, level + LiPo curve for %

# Mic — AudioFeed returns PCM in signal_power field
# int16 PCM at 15625Hz, resampled to 16000Hz on NUC (see audio_client.py)
# ALSA does NOT work on Vector — audio is ADSP-based
```

## Migration Path — COMPLETED

All phases complete as of 2026-03-14. See `docs/ROADMAP.md` for detailed issue tracking.

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Basic Control (gRPC, motors, LEDs, sensors, bridge) | Done |
| 2 | Vision Pipeline (camera, YOLO, face rec, scene) | Done |
| 3 | Person Following (P controller, head tracking, obstacles) | Done |
| 4 | Voice Pipeline (Porcupine, wire-pod Vosk, voice proxy) | Done |
| 5 | Signal/OpenClaw Integration (skills, intercom, LiveKit) | Done |
| 6 | Advanced Features (SLAM, expressions, navigation) | Done |
| 7 | Autonomous Behaviors (explorer, patrol, dead reckoning) | Done |
| 8 | OpenClaw Skills (companion, fitness, monarch-money) | Done |

## Open Questions — ANSWERED

1. **Mic streaming reliability** — Solved. SDK `AudioFeed` `signal_power` field provides int16 PCM at 15625Hz. Resampled to 16000Hz on NUC. Reliable for wake word detection. Wire-pod chipper tap handles STT audio separately. Two-process architecture avoids ADSP exclusivity problems.
2. **Camera frame rate over WiFi** — 800x600 at ~15fps over WiFi gRPC. Sufficient for YOLO (47ms inference). Ring buffer with polling fallback handles SDK quirks.
3. **wire-pod stability** — Excellent for 24/7 operation. Running continuously since 2026-03-10 with zero issues. Vosk STT is local (no cloud dependency).
4. **Multi-Vector support** — Not tested. SDK uses serial number for connection, so multiple Vectors would need separate config entries and SDK instances. Theoretically possible.
5. **OSKR availability** — DDL appears inactive, but existing OSKR-unlocked Vectors work fine with wire-pod. WireOS community firmware (kercre123/wire-os) provides ongoing updates.
