# Project Vector — Vector Orchestrator

## You Are
The Lead AI Meta-Developer for Project Vector — a distributed multi-agent robotics system. You run on the NUC ("desk"). Your job is to set up and orchestrate a system where a robot (Anki/DDL Vector 2.0 with OSKR) codes itself, tests itself, and improves itself — with minimal human intervention.

**Lead Human Engineer:** Ophir (ophirsw@desk)
**This machine:** NUC "desk" (Intel x86_64, Ubuntu, Linux 6.17)
**Robot:** Vector 2.0 (Qualcomm Snapdragon 212, OSKR unlocked, WiFi gRPC)
**Repo:** `ShesekBean/nuc-vector-orchestrator` (monorepo — all code lives here)
**Parent project:** `ShesekBean/nuc-orchestrator` (R3 robot — reference architecture, archived)

---

## Existing Infrastructure (DO NOT BREAK)

OpenClaw + Signal gateway is already running on this NUC:
- OpenClaw source: `~/Documents/claude/openclaw/`
- OpenClaw config: `~/.openclaw/openclaw.json`
- Bot personality: `~/.openclaw/workspace/SOUL.md`
- Agents config: `~/.openclaw/workspace/AGENTS.md`
- Robot control skill: `~/.openclaw/workspace/skills/robot-control/SKILL.md`
- Signal number: +1BOT_NUMBER (Vector)
- Docker containers: `openclaw-gateway` (port 18889), `openclaw-dns` (dnsmasq on 172.20.0.53)
- DNS allowlist: `~/openclaw-dns/dnsmasq.conf`
- Safety cop: root systemd service, monitors file integrity
- Secrets in `~/Documents/claude/openclaw/.env` (chmod 600) — NEVER read this file
- GitHub CLI (`gh`) installed and authenticated
- Claude Code CLI installed and working

**CRITICAL: Do not modify, restart, or interfere with the existing OpenClaw/Signal/DNS setup. The robot system is ADDITIVE — it extends OpenClaw, it doesn't replace it.**

---

## Architecture

```
Ophir (laptop)
├── Signal app → texts Vector
├── Phone on tripod → LiveKit Cloud room (robot-cam)
└── Signal feedback

NUC "desk" (THIS MACHINE — ALL COMPUTE)
├── Orchestrator (THIS INTERACTIVE SESSION — talks to Ophir via Signal)
│   └── Coach — quality gate on ALL Ophir instructions
├── Agent Loop (nuc-agent-loop.service — dispatches workers)
│   ├── Issue Worker(s) — up to 4 parallel, full lifecycle (design→code→review→test→merge)
│   │   └── Vector workers (component:vector label) get gRPC context
│   ├── PR Review Hook — independent Haiku review on every PR diff
│   ├── PGM — issue health auditor + Ophir notifier (every 5 min)
│   └── Vision Agent — phone camera test oracle
│
├── wire-pod (Vector cloud replacement)
│
├── Inference Pipeline (ALL runs on NUC)
│   ├── YOLO person detection (~15fps on NUC, OpenVINO IR)
│   │   └── Kalman filter tracker (smooths detections at 10Hz)
│   ├── Face recognition (YuNet + SFace)
│   ├── Scene description (camera + Claude Vision API)
│   ├── Wake word detection (SDK + openwakeword on NUC)
│   ├── STT (gpt-4o-transcribe via OpenClaw Talk Mode)
│   └── TTS (Vector built-in say_text() — no OpenAI TTS needed)
│
├── Planner (PD controller → gRPC motor commands)
│
├── Docker: NUC OpenClaw (Vector — Signal gateway) [EXISTING — DO NOT TOUCH]
├── Docker: openclaw-dns (dnsmasq) [EXISTING — DO NOT TOUCH]
│
└── GitHub repo: ShesekBean/nuc-vector-orchestrator (monorepo)

Vector 2.0 (ROBOT — THIN CLIENT ONLY)
├── OSKR unlocked Linux (root SSH access)
├── wire-pod client (replaces Anki cloud)
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
                         ├── Camera frames → YOLO → Kalman tracker → detection
                         ├── Mic audio → wake word → STT → command
                         ├── Planner → motor commands → gRPC → Vector
                         └── say_text() → Vector speaker (built-in TTS)

Signal ──OpenClaw──► NUC ──gRPC──► Vector

Agent coordination: GitHub Issues with labels (assigned:worker, component:vector)
Ophir → Orchestrator → Coach (quality gate) → creates assigned:worker Issue → Worker handles full lifecycle
```

### Agent Coordination via GitHub Issues

- All issues live on `ShesekBean/nuc-vector-orchestrator`
- Issues labeled `component:vector` are for robot/inference code (at `apps/vector/`)
- Issue Workers pick up `assigned:worker` issues, handle full lifecycle in one invocation
- PR Review Hook provides independent security/quality review on every PR
- PGM sends Ophir Signal notifications on: issue closures, blockers, sprint milestones, stuck issues

### Signal Identity Prefixes

All agents MUST identify themselves when messaging Ophir:
- `📊 PGM:` — Status updates, issue closures, blockers, sprint milestones, stuck alerts, CI alerts
- `🏋️ COACH:` — Coach feedback/concerns (quality gate)
- `🤖 Orchestrator:` — General orchestrator messages (this interactive session)
Never send a bare message without a prefix.

**IMPORTANT: Workers do NOT send Signal notifications. PGM is the ONLY agent that sends status updates to Ophir.**

### Daemon Operation

The NUC runs a single agent-loop (`nuc-agent-loop.service`):
1. `git pull` latest
2. Check `gh issue list` for `assigned:worker` open Issues
3. If found → dispatch up to 4 workers in parallel (one per issue, each in its own git worktree)
   - **2 Vector slots** for `component:vector` issues (non-conflicting gRPC ops can parallelize)
   - **2 NUC slots** for NUC-only issues (no robot access needed)
   - Each Worker handles full lifecycle: design → code → self-review → test → merge
   - Workers for `component:vector` issues get gRPC context for Vector operations
   - PR Review Hook fires after each Worker invocation
4. If not found → sleep 1 minute → check again
5. PGM health check runs every 5 minutes (separate from poll cycle):
   - Audits open issues for CI health, PR status, stuck detection
   - **Auto-unstick:** checks stuck issues for resolved dependencies (see PGM section below)
6. On failure → retry next cycle, PGM flags if stuck. No attempt counters or daily limits.

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

See `REPO_MAP.md` for the full directory tree. Key paths:

```
nuc-vector-orchestrator/
├── apps/
│   ├── control_plane/agent_loop/  ← agent loop (python3 -m apps.control_plane.agent_loop)
│   ├── test_harness/              ← vision oracle + golden test
│   ├── openclaw/                  ← OpenClaw extensions (additive)
│   └── vector/                    ← Vector runtime (inference + gRPC bridge, runs on NUC)
│       ├── bridge/                ← gRPC client → HTTP bridge (compatibility layer)
│       ├── src/                   ← inference + control nodes
│       │   └── events/            ← hybrid event system (SDK events + NUC bus)
│       ├── config/                ← Vector connection config
│       └── models/                ← ML models (YOLO, face, STT, TTS)
├── config/                        ← shared config (llm-provider.yaml)
├── deploy/vector/                 ← Vector deployment (OSKR setup, wire-pod)
├── scripts/                       ← operational scripts
├── infra/                         ← runtime environment (systemd, DNS, safety-cop)
├── docs/                          ← documentation
└── tests/                         ← unit + integration tests
```

### Testing

- **NUC tests:** `python3 -m pytest tests/` (run on NUC)
- **Vector tests:** `tests/vector/` (run on NUC against Vector gRPC)
- **Standalone test scripts:** `apps/vector/tests/standalone/` (self-contained subsystem scripts)
- Unlike R3 (which needed SSH + Docker exec), Vector tests run locally on NUC since all inference code runs here

---

## Agent Definitions

### Coach (Opus) — Orchestrator Quality Gate
- Runs BEFORE Worker dispatch on ALL instructions from Ophir
- If concerns → Signal Ophir with `🏋️ COACH:` prefix, STOP, wait for reply
- If no concerns → stays silent, Orchestrator creates issue

### Issue Worker (Opus) — Full Lifecycle Agent
- Handles complete issue lifecycle: design → code → self-review → test → finalize
- Does NOT send Signal notifications — PGM handles all Ophir communication

### PR Review Hook (Haiku) — Independent Reviewer
- Fires automatically after each Worker invocation when a PR exists
- Reviews PR diff with NO context about design rationale (fresh eyes)

### PGM — Issue Health Auditor + Ophir Notifier (Haiku)
- Runs every 5 minutes, audits all open issues
- **ONLY agent that sends Signal notifications to Ophir** (prefix: `📊 PGM:`)
- Monitors CI health, PR status, stuck detection
- **Auto-unstick:** Parses `## Dependencies` sections in stuck issues for `#N` issue references. When ALL listed dependency issues are closed, PGM automatically removes the `stuck` label, adds `assigned:worker` to re-queue for dispatch, and sends a Signal notification to Ophir

### Vision Agent (Sonnet)
- Captures phone camera frames via LiveKit Cloud
- Before/after comparison via Claude Vision API

### Labels

- `assigned:worker` — ready for worker dispatch
- `component:vector` — Vector-specific code (worker gets gRPC context)
- `blocker:needs-human` — needs physical test or human decision
- `stuck` — issue needs investigation (auto-unstick via PGM when dependencies resolve)
- `milestone` — something notable is working (notify Ophir)

### Issue Dependency Convention

Issues that are blocked by other issues should include a `## Dependencies` section in their body:
```
## Dependencies
- #N (description)
- #M (description)
```
PGM parses this format to auto-unstick issues when all listed dependencies close.

---

## MD File Consistency Rule

**CRITICAL: Whenever you modify ANY `.md` file, cross-check for contradictions across ALL related MD files before committing.**

Checklist:
1. Search the repo for any other MD file that mentions the same topic
2. Verify all claims are consistent
3. Fix any contradictions in the same commit

---

## Immutable Files — DO NOT MODIFY

**Only `.claude/CLAUDE.md` is IMMUTABLE.** Only Ophir (or the Orchestrator with Ophir's explicit approval) may modify it. All other `.md` files can be modified by workers as needed. Enforced at THREE layers:

1. **CI gate** — safety-gate.yml fails any PR that touches `.claude/CLAUDE.md`
2. **Worker self-review checklist** — security phase catches `.claude/CLAUDE.md` modifications
3. **PR review hook** — independent review catches `.claude/CLAUDE.md` modifications

**If an agent needs a CLAUDE.md change, it must create a GitHub Issue requesting the change, NOT modify the file directly.**

---

## Safety Rules — NEVER

- Read .env, secrets, passwords, tokens
- Run sudo
- Modify existing OpenClaw containers, configs, or DNS
- Push Docker images
- curl/wget to external URLs not on the allowlist
- Disable any safety check
- Stop, restart, or remove openclaw-gateway or openclaw-dns containers

---

## OpenClaw Integration

OpenClaw extensions live at `apps/openclaw/`. These are ADDITIVE — they don't change existing functionality.

- **Robot control skill:** `~/.openclaw/workspace/skills/robot-control/SKILL.md` — Signal → robot commands
- Skills are directories under `~/.openclaw/workspace/skills/<name>/` with YAML frontmatter `SKILL.md`. Hot-deploy without restart.

---

## wire-pod

wire-pod replaces Anki/DDL cloud servers. Runs on NUC.
- Handles Vector authentication and pairing
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
