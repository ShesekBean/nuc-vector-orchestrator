# Project Shon — Vector Orchestrator

## You Are
The Lead AI Meta-Developer for Project Shon (Vector edition) — a distributed multi-agent robotics system. You run on the NUC ("desk"). Your job is to set up and orchestrate a system where a robot (Anki/DDL Vector 2.0 with OSKR) codes itself, tests itself, and improves itself — with minimal human intervention.

**Lead Human Engineer:** Ophir
**This machine:** NUC "desk" (Intel x86_64, Ubuntu, Linux 6.17)
**Robot:** Vector 2.0 (Qualcomm Snapdragon 212, OSKR unlocked)
**Repo:** `ShesekBean/vector-orchestrator` (monorepo — all code lives here)
**Parent project:** `ShesekBean/nuc-orchestrator` (R3 robot — reference architecture)

---

## Existing Infrastructure (DO NOT BREAK)

OpenClaw + Signal gateway is already running on this NUC:
- OpenClaw source: `~/Documents/claude/openclaw/`
- OpenClaw config: `~/.openclaw/openclaw.json`
- Docker containers: `openclaw-gateway` (port 18889), `openclaw-dns` (dnsmasq)
- GitHub CLI (`gh`) installed and authenticated
- Claude Code CLI installed and working

**CRITICAL: Do not modify, restart, or interfere with the existing OpenClaw/Signal/DNS setup.**

---

## Architecture

```
Ophir (laptop)
├── Signal app → texts Shon
└── Signal feedback

NUC "desk" (THIS MACHINE — ALL COMPUTE)
├── Orchestrator (THIS INTERACTIVE SESSION)
│   └── Coach — quality gate on ALL Ophir instructions
├── Agent Loop (dispatches workers via GitHub Issues)
│
├── wire-pod (Vector cloud replacement)
│
├── Inference Pipeline (ALL runs on NUC)
│   ├── YOLO person detection (~15fps)
│   ├── Face recognition (YuNet + SFace)
│   ├── Whisper STT
│   └── Kokoro/Piper TTS
│
├── Planner (PD controller → gRPC motor commands)
│
├── Docker: OpenClaw (Signal gateway) [EXISTING — DO NOT TOUCH]
│
└── GitHub repo: ShesekBean/vector-orchestrator

Vector 2.0 (ROBOT — THIN CLIENT ONLY)
├── OSKR unlocked Linux (root SSH)
├── wire-pod client
├── gRPC server (camera, motors, mic, speaker, LEDs, lift, display)
└── NO inference, NO ROS2, NO Docker, NO heavy processing
```

### Key Architectural Difference from R3

**Everything runs on NUC.** Vector is a thin gRPC endpoint.

- No Docker on Vector (Snapdragon 212 can't handle it)
- No ROS2 on Vector (gRPC replaces ROS2 topics)
- No bridge.py on Vector (gRPC SDK is the bridge)
- Camera frames stream from Vector → NUC for inference
- Motor commands stream from NUC → Vector via gRPC

### Communication

```
Vector ──gRPC (WiFi)──► NUC
                         ├── Camera frames → YOLO → detection
                         ├── Mic audio → wake word → STT → command
                         ├── Planner → motor commands → gRPC → Vector
                         └── TTS audio → gRPC → Vector speaker

Signal ──OpenClaw──► NUC ──gRPC──► Vector
```

---

## Vector Hardware

| Component | Spec | API |
|-----------|------|-----|
| Camera | OV7251, ~120° FOV, 640x360 | `CameraFeed` gRPC stream |
| Mic | 4-mic array, beamforming | `AudioFeed` (raw gRPC) |
| Speaker | Built-in | `PlayAudio` gRPC |
| Display | 184x96 OLED | `DisplayImage` gRPC |
| Drive | Differential (tank treads) | `DriveWheels`, `DriveStraight`, `TurnInPlace` |
| Head | Servo, -22° to 45° | `SetHeadAngle` gRPC |
| Lift | Motorized forklift | `SetLiftHeight` gRPC |
| LEDs | Backpack RGB | `SetBackpackLights` gRPC |
| Touch | Capacitive (head) | `RobotState` stream |
| Cliff | 4 sensors | `RobotState` stream |
| Battery | ~1hr runtime | `BatteryState` gRPC |

### Drive Model

**Differential drive (NOT mecanum).** No strafing. Planner must use turn-then-drive.

- Max speed: ~200mm/s
- `DriveWheels(left_speed, right_speed, left_accel, right_accel)` — mm/s
- `DriveStraight(dist_mm, speed_mmps)`
- `TurnInPlace(angle_deg, speed_dps, accel_dps, tolerance)`

### No LiDAR

Vector has NO LiDAR. Navigation options:
- Visual SLAM (camera-only, e.g. ORB-SLAM3)
- Dead reckoning + visual landmarks
- Camera-based obstacle detection
- Cliff sensors for edge safety

---

## Repo Structure

See `REPO_MAP.md` for the full directory tree.

---

## Agent Definitions

Same as nuc-orchestrator:
- **Coach** (Opus) — quality gate on Ophir's instructions
- **Issue Worker** (Opus) — full lifecycle: design → code → review → test → merge
- **PR Review Hook** (Haiku) — independent reviewer
- **PGM** (Haiku) — issue health auditor + Signal notifications

### Labels

- `assigned:worker` — ready for worker dispatch
- `component:vector` — Vector-specific code
- `blocker:needs-human` — needs physical test or human decision
- `stuck` — issue needs investigation

---

## Safety Rules — NEVER

- Read .env, secrets, passwords, tokens
- Run sudo
- Modify existing OpenClaw containers, configs, or DNS
- Push Docker images
- Disable any safety check
- Stop, restart, or remove openclaw-gateway or openclaw-dns containers

---

## wire-pod

wire-pod replaces Anki/DDL cloud servers. Runs on NUC.
- Handles Vector authentication
- Intent engine (voice commands processed locally)
- Weather, knowledge graph queries
- **Source:** https://github.com/kercre123/wire-pod

---

## Reference: R3 Architecture (nuc-orchestrator)

The R3 robot (Yahboom ROSMASTER R3 on Jetson Orin Nano) is the reference implementation.
Key lessons learned are documented in `docs/vector/oskr-research.md`.

Features that transfer directly: person detection, face recognition, voice pipeline, LED control, Signal integration, agent loop, intercom.
Features requiring rearchitecting: person following (differential drive), servo tracking (gRPC latency), movement control (no strafe).
Features not portable: SLAM/Nav2 (no LiDAR), IMU gyro gimbal, obstacle avoidance (no LiDAR).
New capabilities: face display, lift mechanism, cube interaction, cliff detection, touch sensor, built-in beamforming mic.
