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
nuc-vector-orchestrator/
├── apps/                          ← runnable applications
│   ├── control_plane/
│   │   └── agent_loop/            ← main agent loop (dispatches workers)
│   │       ├── __main__.py        ← entry point: python3 -m apps.control_plane.agent_loop
│   │       ├── account_rotation.py ← multi-account quota rotation
│   │       ├── board.py           ← board/dispatch state tracking
│   │       ├── config.py          ← configuration + LLM provider parsing
│   │       ├── dispatch.py        ← worker dispatch, PR review, merge gate
│   │       ├── github.py          ← GitHub API wrapper (gh CLI)
│   │       ├── inbox.py           ← Signal inbox processing
│   │       ├── llm.py             ← LLM invocation (claude CLI)
│   │       ├── log.py             ← logging utilities
│   │       ├── loop.py            ← main loop class
│   │       ├── pgm.py             ← issue health auditor + Signal notifications
│   │       ├── signal_client.py   ← Signal message sending
│   │       ├── state.py           ← TSV state file management
│   │       └── watchdog.py        ← worker timeout watchdog
│   ├── test_harness/              ← vision oracle + automated testing
│   │   ├── action_evaluator.py    ← action evaluation logic
│   │   ├── automated_test.py     ← automated test runner
│   │   ├── camera_capture.py     ← camera frame capture
│   │   ├── evolution_agent.py    ← evolution/experiment agent
│   │   ├── golden_test_r3_archived.py ← R3 golden test (archived, replaced by tests/golden/)
│   │   └── vision_config.yaml    ← vision pipeline configuration
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
│   │   │   ├── companion/
│   │   │   │   └── SKILL.md       ← Companion personality (presence signals → speech/expression/movement)
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
│       ├── __main__.py            ← entry point: python3 -m apps.vector
│       ├── supervisor.py          ← component lifecycle manager (ordered startup, health, reconnect)
│       ├── bridge/                ← gRPC client → HTTP bridge (compatibility layer)
│       ├── src/                   ← inference + control nodes
│       │   ├── detector/          ← YOLO person detection (NUC GPU/CPU)
│       │   ├── face_recognition/  ← YuNet + SFace (NUC)
│       │   ├── planner/           ← P controller + drive+steer → gRPC motor commands
│       │   │   ├── __init__.py
│       │   │   ├── follow_planner.py ← person-following planner
│       │   │   ├── head_tracker.py   ← head servo tracking logic
│       │   │   ├── obstacle_detector.py ← camera-based obstacle detection
│       │   │   └── visual_slam.py ← monocular ORB-SLAM for camera-only navigation
│       │   ├── livekit_bridge.py   ← LiveKit WebRTC bridge (camera+mic out, audio+video in; mic via wire-pod chipper tap)
│       │   ├── voice/             ← wake word + OpenClaw Talk Mode bridge (NUC)
│       │   │   ├── audio_client.py ← Vector mic gRPC consumer (resamples 15625→16000 Hz, ring buffer)
│       │   │   ├── echo_cancel.py ← echo cancellation for mic input
│       │   │   ├── message_relay.py ← voice message relay
│       │   │   ├── openclaw_voice_bridge.py ← OpenClaw voice integration
│       │   │   ├── speech_output.py ← TTS speech output
│       │   │   ├── voice_command_router.py ← voice command routing
│       │   │   └── wake_word.py   ← wake word detection
│       │   ├── camera/            ← gRPC camera feed consumer + scene description
│       │   │   ├── camera_client.py  ← camera frame streaming
│       │   │   ├── camera_benchmark.py ← camera performance benchmarking
│       │   │   └── scene_describer.py ← Claude Vision scene description
│       │   ├── display_controller.py ← OLED display image/text rendering
│       │   ├── head_controller.py  ← head servo angle control with safety clamping
│       │   ├── led_controller.py   ← eye color + animated LED patterns (priority-based state manager)
│       │   ├── lift_controller.py  ← forklift height control with named presets
│       │   ├── motor_controller.py ← cliff-safe differential drive (turn-then-drive planner)
│       │   ├── companion/           ← companion behavior system (presence → OpenClaw personality)
│       │   │   ├── __init__.py      ← CompanionSystem lifecycle manager
│       │   │   ├── presence_tracker.py ← state machine: detection events → arrival/departure/check-in
│       │   │   ├── dispatcher.py    ← engagement-adaptive throttling + OpenClaw signal formatting
│       │   │   └── openclaw_client.py ← WebSocket chat.send client (companion session)
│       │   ├── expression_engine.py ← coordinated face + LED + sound expressions (emotion states)
│       │   ├── intercom.py        ← Signal messaging client (photo + text to Ophir via intercom-server)
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
│       ├── native/                ← native C binaries (cross-compiled for Vector ARM)
│       │   └── vector-streamer/   ← mic/engine DGRAM proxy + TCP bridge to NUC
│       │       ├── main.c         ← entry point, TCP server, Opus encode
│       │       ├── mic.c/h        ← server-side DGRAM proxy for mic_sock
│       │       ├── engine_proxy.c/h ← engine-anim DGRAM proxy (deferred ANKICONN)
│       │       ├── tcp_server.c/h ← framed TCP protocol (port 5555)
│       │       ├── opus_encoder.c/h ← Opus encoding wrapper
│       │       ├── protocol.h     ← frame types (OPUS, H264, JPEG, PCM, CMD)
│       │       ├── bin/           ← pre-built ARM binary
│       │       ├── victor-reference/ ← backed-up Victor CLAD/source for reference
│       │       ├── CMakeLists.txt ← cross-compile config
│       │       └── vicos-toolchain.cmake ← vicos-sdk toolchain
│       ├── config/                ← Vector connection config
│       └── models/                ← ML models (YOLO, face)
├── config/                        ← shared cross-component configuration
│   └── llm-provider.yaml         ← LLM provider + model selection
├── deploy/
│   └── vector/                    ← Vector deployment (OSKR setup, wire-pod)
├── scripts/                       ← operational utility scripts
│   ├── wire-pod-setup.sh         ← wire-pod installation on NUC
│   ├── vector-connect.sh         ← Vector gRPC connectivity test
│   ├── wire-pod-start.sh          ← wire-pod launcher for systemd
│   ├── openclaw-voice-proxy.py   ← Wire-pod → OpenClaw voice bridge (standalone script, OpenAI API, voice context prefix)
│   ├── sprint-end.sh             ← end-of-sprint test/backup workflow
│   ├── pgm-signal-gate.sh        ← rate-limited Signal notifications
│   ├── signal-interactive.sh     ← interactive test Signal library
│   ├── intercom-server.py        ← NUC HTTP intercom relay → Signal DM
│   ├── monarch-login.py          ← Monarch Money auth → saves token for OpenClaw
│   └── ...                        ← other utility scripts
├── infra/                         ← runtime environment configuration
│   ├── vector/                    ← Vector infra (services, wire-pod config, OSKR setup)
│   │   ├── vector-streamer.service ← systemd service (Before=vic-engine, After=vic-anim)
│   │   ├── vector-streamer-start.sh ← startup script (socket wait, launch, verify)
│   │   └── web-setup/            ← Local mirror of vector-web-setup.anki.bot (BLE pairing)
│   ├── docker/                    ← Signal gateway Docker setup
│   ├── systemd/                   ← Service units (wire-pod, quiet mode, supervisor, bridge, agent loop)
│   ├── dns/                       ← NUC dnsmasq
│   └── safety-cop/                ← NUC safety_cop.py
├── docs/                          ← documentation
│   ├── vector/                    ← Vector-specific docs
│   │   └── oskr-research.md      ← OSKR SDK research & feature portability
│   ├── architecture/              ← architecture docs
│   └── runbooks/                  ← operational runbooks
├── tests/                         ← unit + integration tests
│   ├── golden/                    ← phase-gated golden test suite (pytest)
│   │   ├── conftest.py            ← shared fixtures (Vector connection, phase gating)
│   │   ├── test_phase0_unit.py    ← unit tests (no hardware)
│   │   ├── test_phase1_preflight.py ← preflight checks (connectivity, wire-pod)
│   │   ├── test_phase2_hardware.py  ← hardware validation (camera, sensors, battery)
│   │   ├── test_phase3_inference.py ← inference pipeline (YOLO, face recognition)
│   │   ├── test_phase4_voice.py     ← voice pipeline (wake word, STT, TTS)
│   │   ├── test_phase5_movement.py  ← movement tests (motors, head, lift)
│   │   ├── test_phase6_following.py ← person-following pipeline
│   │   ├── test_phase7_signal.py    ← Signal → OpenClaw → robot E2E
│   │   ├── test_phase8_agentloop.py ← agent loop health checks
│   │   ├── test_phase9_eventbus.py  ← event bus integration
│   │   ├── test_phase10_services.py ← systemd service health checks
│   │   ├── test_phase12_signal_robot.py ← Signal → robot E2E integration
│   │   └── test_phase13_livekit.py  ← LiveKit video bridge tests
│   └── vector/                    ← Vector-specific tests
│       └── test_intercom.py       ← intercom module tests (photo + text messaging)
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
2. **wire-pod**: Native service on NUC (systemd: `wire-pod.service`, root)
3. **Voice Proxy**: `python3 scripts/openclaw-voice-proxy.py` (standalone script) — bridges wire-pod STT → OpenClaw LLM
4. **Vector Supervisor**: `python3 -m apps.vector` (component lifecycle: startup, health, reconnect)
5. **Vector Bridge**: `python3 -m apps.vector.bridge` (gRPC → HTTP compatibility; connects with OVERRIDE_BEHAVIORS — Vector sits still by default; `POST /mode {"mode":"playful"}` to release)
7. **Process Management**: `bash scripts/start-all.sh` / `bash scripts/kill-all.sh`

## Testing

Vector tests exist in two locations:

- **`tests/vector/`** — pytest-based integration tests (run with `pytest tests/vector/ -v`)
- **`apps/vector/tests/standalone/`** — standalone scripts for subsystem validation (run individually, e.g. `python3 apps/vector/tests/standalone/test_camera.py`)

Standalone scripts are self-contained: each connects to Vector directly and validates a single subsystem (camera, motors, head, lift, LEDs, display, audio, sensors, detection, follow). Use these for quick hardware validation without the full test harness.
