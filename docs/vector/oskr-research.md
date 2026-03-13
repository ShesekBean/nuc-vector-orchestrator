# Vector 2.0 OSKR — Platform Research & Feature Portability

**Date:** 2026-03-10
**Source:** SDK analysis of [wire-pod](https://github.com/kercre123/wire-pod) and [vector-go-sdk](https://github.com/digital-dream-labs/vector-go-sdk)

---

## Platform Overview

| Spec | Yahboom ROSMASTER R3 (current) | Anki/DDL Vector 2.0 OSKR |
|------|-------------------------------|--------------------------|
| CPU | Jetson Orin Nano Super (6-core A78AE + GPU) | Qualcomm Snapdragon 212 (4-core A7, 1.3GHz) |
| RAM | 8GB | 1GB |
| Camera | IMX219 CSI, 160° fisheye, 1280x720 | OV7251, ~120° FOV, 640x360 |
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

### Fully Portable (minimal changes)

| Feature | Current (R3) | Vector Port | Notes |
|---------|-------------|-------------|-------|
| Person detection (YOLO) | Jetson GPU, ~3Hz | NUC GPU/CPU, ~15+ fps | Actually faster — no Jetson GPU contention |
| Face recognition | YuNet + SFace on Jetson | Same models on NUC | Better — NUC has more compute |
| Voice pipeline | Wake word → STT → Command → TTS | Wake word → OpenClaw Talk Mode (gpt-4o-transcribe STT → Vector agent → say_text()) | 4-mic array + accent-friendly STT via OpenAI OAuth |
| LED control | Rosmaster_Lib API | gRPC `SetBackpackLights` | Different API, same concept |
| Text-to-speech | Kokoro/Piper on Jetson | say_text() (Vector built-in TTS) | Onboard, zero cost, no audio streaming needed |
| Signal integration | OpenClaw → bridge HTTP | OpenClaw → gRPC | Same architecture |
| Intercom (text/photo) | Bridge → NUC HTTP → Signal | gRPC → NUC → Signal | Simpler — no Jetson bridge needed |
| Agent loop / workers | GitHub Issues dispatch | Identical | No changes needed |

### Portable with Rearchitecting

| Feature | Current (R3) | Vector Challenge | Mitigation |
|---------|-------------|-----------------|------------|
| Person following | PD controller, ~2Hz YOLO | WiFi latency adds ~50-100ms | Vector moves slowly (~200mm/s) — latency is acceptable |
| Servo tracking | Direct PWM, 50Hz updates | gRPC head motor, WiFi latency | Head moves slowly anyway; ~10Hz updates sufficient |
| Movement control | Mecanum (omnidirectional strafe) | Differential drive (no strafe) | Rewrite planner for turn-then-drive |
| Camera streaming | CSI direct, 720p | gRPC `CameraFeed`, 640x360 | Lower res but sufficient for YOLO (letterboxes to 640x640) |
| Scene description | YOLO on Jetson GPU | YOLO on NUC | Frame transfer over WiFi, ~50ms |

### Not Portable (missing hardware)

| Feature | Current (R3) | Vector Limitation | Alternative |
|---------|-------------|-------------------|-------------|
| SLAM / Nav2 | RPLIDAR A1 360° scan | No LiDAR | Visual SLAM only (ORB-SLAM3 with camera) |
| Waypoint navigation | Nav2 + SLAM map | No LiDAR for costmap | Dead reckoning + visual landmarks |
| Obstacle avoidance | LiDAR forward cone | ToF + cliff sensors only | Camera-based obstacle detection |
| IMU gyro gimbal | Direct IMU read, 50Hz | No exposed IMU API | Camera-based stabilization or skip |
| Battery monitoring | MCU voltage read | gRPC `BatteryState` | Different API but available |

### New Capabilities (Vector has, R3 doesn't)

| Feature | Description |
|---------|-------------|
| Face display | 160x80 OLED (Vector 2.0 Xray; SDK sends 184x96, vic-engine converts stride) — face animations, status display, emoji |
| Lift mechanism | Motorized forklift — can pick up small objects (Vector's cube) |
| Cube interaction | Detects and interacts with light cube (tap, roll, stack) |
| Cliff detection | 4 cliff sensors — table-safe operation |
| Capacitive touch | Top-of-head touch sensor |
| Built-in beamforming | 4-mic array with hardware beamforming > USB mic |

## Architecture: Vector Orchestrator

```
Ophir (laptop)
├── Signal app → texts Vector
└── Signal feedback

NUC "desk" (THIS MACHINE — ALL COMPUTE)
├── Orchestrator (interactive session)
├── Agent Loop (dispatches workers)
├── wire-pod (Vector cloud replacement)
├── Inference Pipeline
│   ├── YOLO person detection (~15fps on NUC)
│   ├── Face recognition (YuNet + SFace)
│   ├── STT (gpt-4o-transcribe via OpenClaw Talk Mode)
│   └── TTS (Vector built-in say_text() — no OpenAI TTS needed)
├── Planner (PD controller → gRPC motor commands)
├── Docker: OpenClaw (Signal gateway)
└── GitHub repo: ShesekBean/nuc-vector-orchestrator

Vector 2.0 (ROBOT — thin client)
├── OSKR unlocked Linux
├── wire-pod client (replaces Anki cloud)
├── gRPC server (camera, motors, mic, speaker, LEDs, lift)
└── No inference, no ROS2, no heavy processing
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
Vector ──gRPC──► NUC (wire-pod + inference + planner)
                  │
                  ├── Camera frames → YOLO → detection
                  ├── Mic audio → wake word → STT → command
                  ├── Planner → motor commands → gRPC → Vector
                  └── say_text() → Vector speaker (built-in TTS)
```

## Latency Analysis

| Pipeline | R3 (local) | Vector (WiFi) | Acceptable? |
|----------|-----------|---------------|-------------|
| Camera → YOLO → detection | ~300ms | ~350-400ms | Yes — Vector moves slowly |
| Detection → motor command | <10ms | ~50ms | Yes |
| Full tracking loop | ~350ms | ~450ms | Yes — 2Hz is fine for ~200mm/s robot |
| Wake word → STT → command | ~2.5s | ~2.5s (same) | Yes — already on NUC |
| TTS → speaker | ~1s | ~1.2s | Yes |
| Camera → photo → Signal | ~3s | ~3.5s | Yes |

**Conclusion:** WiFi latency adds ~50-100ms per hop. Since Vector's max speed is ~200mm/s (vs R3's ~500mm/s), the tracking loop is still responsive enough. Voice pipeline is identical since STT/TTS already ran on NUC.

## gRPC SDK Quick Reference

```go
// Camera feed
client.CameraFeed(ctx) // returns stream of JPEG frames

// Motors
client.DriveWheels(ctx, leftSpeed, rightSpeed, leftAccel, rightAccel) // mm/s
client.DriveStraight(ctx, distMM, speedMMPS)
client.TurnInPlace(ctx, angleDeg, speedDPS, accelDPS, tolerance)

// Head & Lift
client.MoveHead(ctx, speedRadPS) // -1.0 to 1.0
client.MoveLift(ctx, speedRadPS) // -1.0 to 1.0
client.SetHeadAngle(ctx, angleDeg, speedDPS) // -22° to 45°
client.SetLiftHeight(ctx, heightMM, speedMMPS)

// LEDs
client.SetBackpackLights(ctx, front, middle, back) // each has RGBA

// Audio
client.PlayAudio(ctx, audioData) // WAV bytes

// Display
client.DisplayImage(ctx, imageBytes) // 160x80 OLED (SDK accepts 184x96; vic-engine converts)

// Sensors
client.BatteryState(ctx) // voltage, charging, level
client.RobotState(ctx) // accel, gyro, cliff sensors, touch

// Mic (raw gRPC, not in SDK wrapper)
// AudioFeed stream available at protocol level
```

## Migration Path

### Phase 1: Basic Control
- Set up wire-pod on NUC
- OSKR unlock Vector
- gRPC connectivity test (motors, LEDs, camera)
- Port OpenClaw robot-control skill (HTTP → gRPC)

### Phase 2: Vision Pipeline
- Camera streaming NUC → YOLO detection
- Person detection + face recognition
- Scene description

### Phase 3: Person Following
- Rewrite planner for differential drive (no strafe)
- Camera frame → NUC YOLO → gRPC motor command loop
- Head tracking via `SetHeadAngle`

### Phase 4: Voice Pipeline
- Mic audio streaming (raw gRPC implementation needed)
- Wake word + STT on NUC
- Command routing
- TTS → gRPC audio playback

### Phase 5: Advanced Features
- Visual SLAM (camera-only, no LiDAR)
- Face display animations
- Lift/cube interaction
- Touch sensor events

## Open Questions

1. **Mic streaming reliability** — SDK wrapper removed this; raw gRPC implementation untested
2. **Camera frame rate over WiFi** — need to benchmark actual throughput
3. **wire-pod stability** — community-maintained, how robust for 24/7 operation?
4. **Multi-Vector support** — can one NUC control multiple Vectors?
5. **OSKR availability** — DDL's OSKR program status unclear as of 2026
