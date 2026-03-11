# Repository Map — Vector Orchestrator

Read this file first before exploring the repository.

## Architecture

This repo follows the same multi-plane architecture as nuc-orchestrator, adapted for Vector 2.0:

- **Control Plane** (NUC) — deploys, orchestrates, runs ALL inference
- **Testing Plane** (NUC) — validates behavior
- **Runtime Plane** (Vector) — thin gRPC client, motors/sensors/camera only

Key difference from R3: **no Docker on Vector, no ROS2.** All compute runs on NUC.
Vector communicates exclusively via gRPC over WiFi.

## Directory Structure

```
vector-orchestrator/
├── apps/                          ← runnable applications
│   ├── control_plane/
│   │   └── agent_loop/            ← main agent loop (dispatches workers)
│   │       ├── __main__.py        ← entry point: python3 -m apps.control_plane.agent_loop
│   │       ├── config.py          ← configuration + LLM provider parsing
│   │       ├── dispatch.py        ← worker dispatch, PR review, merge gate
│   │       ├── github.py          ← GitHub API wrapper (gh CLI)
│   │       ├── inbox.py           ← Signal inbox processing
│   │       ├── llm.py             ← LLM invocation (claude CLI)
│   │       ├── loop.py            ← main loop class
│   │       ├── pgm.py             ← issue health auditor + Signal notifications
│   │       ├── signal_client.py   ← Signal message sending
│   │       └── state.py           ← TSV state file management
│   ├── test_harness/              ← vision oracle + automated testing
│   │   ├── golden_test.py         ← comprehensive test
│   │   └── voice_commands/        ← test WAV files
│   ├── openclaw/                  ← OpenClaw config backup + extensions (additive)
│   │   ├── docker/                ← Container run scripts (sanitized, tokens via env vars)
│   │   │   ├── openclaw-gateway-run.sh  ← Signal gateway container
│   │   │   └── openclaw-ade-run.sh      ← ADE (work bot) container
│   │   ├── config/
│   │   │   ├── openclaw.json.example    ← Full config template (secrets redacted)
│   │   │   └── signal-config.json.example ← Signal channel config reference
│   │   ├── cron/
│   │   │   └── jobs.json          ← Cron job definitions (fitness, briefs)
│   │   ├── workspace/             ← Workspace MD files (SOUL, IDENTITY, AGENTS, etc.)
│   │   ├── skills/
│   │   │   ├── robot-control/
│   │   │   │   └── SKILL.md       ← Signal → robot commands
│   │   │   ├── fitness/
│   │   │   │   └── SKILL.md       ← Fitness tracking skill
│   │   │   └── monarch-money/
│   │   │       └── SKILL.md       ← Monarch Money financial queries (read-only)
│   │   ├── agent-notifier.js      ← Agent notification helper
│   │   ├── command-allowlist.yaml  ← Allowed commands
│   │   ├── intercom-relay.js      ← Intercom message relay
│   │   ├── narration-receiver.js  ← Narration endpoint
│   │   ├── robot-commands.js      ← Robot command dispatcher
│   │   └── voice_chat_relay.py    ← Voice chat relay
│   └── vector/                    ← Vector runtime bridge (runs on NUC)
│       ├── bridge/                ← gRPC client → HTTP bridge (compatibility layer)
│       ├── src/                   ← inference + control nodes
│       │   ├── detector/          ← YOLO person detection (NUC GPU/CPU)
│       │   ├── face_recognition/  ← YuNet + SFace (NUC)
│       │   ├── planner/           ← PD controller → gRPC motor commands
│       │   ├── voice/             ← wake word + OpenClaw Talk Mode bridge (NUC)
│       │   ├── camera/            ← gRPC camera feed consumer
│       │   ├── head_controller.py  ← head servo angle control with safety clamping
│       │   ├── sensor_handler.py  ← cliff detection + touch event handler (safety-critical)
│       │   └── events/            ← hybrid event system (SDK events + NUC bus)
│       │       ├── event_types.py ← event type definitions + priorities
│       │       ├── nuc_event_bus.py ← pub/sub event bus for NUC-side events
│       │       └── sdk_events.py  ← Vector SDK event bridge → NUC bus
│       ├── tests/
│       │   └── standalone/        ← standalone subsystem test scripts (run individually)
│       │       ├── test_camera.py
│       │       ├── test_motors.py
│       │       ├── test_head.py
│       │       ├── test_lift.py
│       │       ├── test_leds.py
│       │       ├── test_display.py
│       │       ├── test_audio.py
│       │       ├── test_mic.py
│       │       ├── test_sensors.py
│       │       ├── test_detection.py
│       │       └── test_follow_standalone.py
│       ├── config/                ← Vector connection config
│       └── models/                ← ML models (YOLO, face)
├── config/                        ← shared cross-component configuration
│   ├── llm-provider.yaml         ← LLM provider + model selection
│   └── sprint-definitions.yaml   ← sprint metadata
├── deploy/
│   └── vector/                    ← Vector deployment (OSKR setup, wire-pod)
├── scripts/                       ← operational utility scripts
│   ├── wire-pod-setup.sh         ← wire-pod installation on NUC
│   ├── vector-connect.sh         ← Vector gRPC connectivity test
│   ├── sprint-end.sh             ← end-of-sprint test/backup workflow
│   ├── pgm-signal-gate.sh        ← rate-limited Signal notifications
│   ├── signal-interactive.sh     ← interactive test Signal library
│   ├── intercom-server.py        ← NUC HTTP intercom relay → Signal DM
│   ├── monarch-login.py          ← Monarch Money auth → saves token for OpenClaw
│   └── ...                        ← other utility scripts
├── infra/                         ← runtime environment configuration
│   ├── vector/                    ← Vector infra (wire-pod config, OSKR setup)
│   ├── docker/                    ← Signal gateway Docker setup
│   ├── systemd/                   ← Service units
│   ├── dns/                       ← NUC dnsmasq
│   └── safety-cop/                ← NUC safety_cop.py
├── docs/                          ← documentation
│   ├── vector/                    ← Vector-specific docs
│   │   └── oskr-research.md      ← OSKR SDK research & feature portability
│   ├── architecture/              ← architecture docs
│   └── runbooks/                  ← operational runbooks
├── tests/                         ← unit + integration tests
│   └── vector/                    ← Vector-specific tests
├── .claude/                       ← Claude Code configuration
│   ├── CLAUDE.md                  ← project config
│   └── agents/                    ← agent role definitions
├── .github/workflows/             ← CI pipelines
└── REPO_MAP.md                    ← this file
```

## Key Differences from nuc-orchestrator (R3)

| Aspect | R3 (nuc-orchestrator) | Vector (this repo) |
|--------|----------------------|-------------------|
| Robot compute | Jetson Orin Nano (GPU) | NUC does everything |
| Communication | SSH + HTTP bridge | gRPC over WiFi |
| Container | Docker on Jetson | No Docker on Vector |
| Framework | ROS2 Humble | Python + gRPC |
| Drive | Mecanum (omnidirectional) | Differential (tank treads) |
| LiDAR | RPLIDAR A1 | None (camera-only) |
| Cloud | None | wire-pod (replaces Anki cloud) |

## Key Entry Points

1. **Agent Loop**: `python3 -m apps.control_plane.agent_loop` (systemd: `nuc-agent-loop.service`)
2. **wire-pod**: Docker container or native service on NUC
3. **Vector Bridge**: `python3 -m apps.vector.bridge` (gRPC → HTTP compatibility)
4. **Process Management**: `bash scripts/start-all.sh` / `bash scripts/kill-all.sh`

## Testing

Vector tests exist in two locations:

- **`tests/vector/`** — pytest-based integration tests (run with `pytest tests/vector/ -v`)
- **`apps/vector/tests/standalone/`** — standalone scripts for subsystem validation (run individually, e.g. `python3 apps/vector/tests/standalone/test_camera.py`)

Standalone scripts are self-contained: each connects to Vector directly and validates a single subsystem (camera, motors, head, lift, LEDs, display, audio, sensors, detection, follow). Use these for quick hardware validation without the full test harness.
