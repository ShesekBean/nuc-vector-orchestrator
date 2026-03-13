# Vector Golden Test Plan

Comprehensive end-to-end test covering all project subsystems in ~3 minutes.

**Goal:** Maximum coverage with minimum tests. Each test covers the most ground possible — no trivial constant checks, no redundant cross-phase tests.

**Run:**
```bash
# Full suite (needs robot + services)
python3 -m pytest tests/golden/ -v

# NUC-only (no robot needed)
python3 -m pytest tests/golden/ -v -k "phase0 or phase3 or phase4 or phase8 or phase9"

# Just new phases
python3 -m pytest tests/golden/ -v -k "phase10 or phase12 or phase13"
```

---

## Phase 0 — NUC Unit Tests (5 tests)

No robot needed. Catches regressions before touching hardware.

| # | Test | Assert |
|---|------|--------|
| 0.1 | Config + dispatch | `Config()` valid, `dispatch_label == "assigned:worker"` |
| 0.2 | LLM config | `parse_llm_config()` returns heavy/medium/light models |
| 0.3 | PGM dependency parser | `## Dependencies` parsed, empty body → empty set |
| 0.4 | Inbox command routing | `#go`, `#approve`, pass/fail all route correctly |
| 0.5 | Signal gate script | `pgm-signal-gate.sh` exists and executable |

---

## Phase 1 — Preflight: Robot Connectivity + Battery Gate (4 tests)

**Gate:** If fails, skip Phases 2, 5, 6. NUC-only phases still run.

| # | Test | Assert |
|---|------|--------|
| 1.1 | SDK connects | `robot.connect()` succeeds, `robot.status` populated |
| 1.2 | Battery level | Battery ≥ LOW (safe for motors) |
| 1.3 | gRPC latency | `get_battery_state()` < 500ms |
| 1.4 | Control handoff | Request → release → re-request, no errors |

---

## Phase 2 — Stationary Hardware (1 batch test, 13 sub-tests)

Robot stays still. Single sequential batch with 5s pauses for observation.

| # | Test | Assert |
|---|------|--------|
| 2.1 | Camera single frame | Image captured, ≥ 320x180 |
| 2.2 | Camera feed stream | 5 unique frames received |
| 2.3–2.5 | Head angles | Min (-22°), max (45°), neutral (10°) |
| 2.6–2.7 | Lift | Up (1.0), down (0.0) |
| 2.8–2.9 | LEDs | Red eyes, then default green |
| 2.11 | TTS | `say_text("test")` plays audio |
| 2.12 | Touch sensor | Returns valid `is_being_touched` state |
| 2.13 | Cliff sensors | `robot.status` not None |
| 2.14 | Accelerometer | 3-axis values, not all zero |
| 2.15 | Gyroscope | 3-axis values returned |

---

## Phase 3 — NUC Inference Pipeline (6 tests)

No robot needed. Tests all CV/ML on NUC with synthetic/mocked data.

| # | Test | Assert |
|---|------|--------|
| 3.1 | YOLO model loads | OpenVINO IR model loads |
| 3.2 | YOLO detect + empty | Person → bbox (conf > 0.5), empty frame → no detections |
| 3.3 | Face recognition | Same person high similarity, different people low similarity |
| 3.4 | Kalman tracker lifecycle | Create → consistent ID over 5 frames → timeout drops track |
| 3.5 | Scene describer | `SceneDescriber` instantiates with mock camera |
| 3.6 | Detection → event bus | YOLO detection fires `person_detected` event |

---

## Phase 4 — Voice Pipeline (3 tests)

| # | Test | Assert |
|---|------|--------|
| 4.1 | Audio stream (robot) | `AudioFeed` gRPC → raw bytes received |
| 4.2 | Wake word detection | Detector instantiates, fires event on detection, no false triggers |
| 4.3 | TTS chunking | `SpeechOutput.chunk_text()` splits correctly |

---

## Phase 5 — Movement + Safety (1 batch test, 10 sub-tests)

**Requires:** Phase 1 passed. Robot on safe surface. 5s pauses between tests.

| # | Test | Assert |
|---|------|--------|
| 5.1–5.2 | Drive forward/backward | 50mm each direction |
| 5.3–5.4 | Turn right/left | 90° each direction |
| 5.5 | Cliff sensor check | Sensors report safe |
| 5.6 | Safety module | `CliffSafetyError` + `MotorController` importable |
| 5.7 | Emergency stop | Wheels to 0 |
| 5.8 | Speed clamp | 500mm/s → clamped to 200mm/s |
| 5.9 | Head stable during move | Head at 10° stays stable during 30mm drive |
| 5.10 | Post-move position | Pose changes after drive |

---

## Phase 6 — Person Following (3 tests)

| # | Test | Assert |
|---|------|--------|
| 6.1 | Follow lifecycle | Start → SEARCHING → stop → IDLE |
| 6.2 | Target re-acquisition | Person disappears 2 frames, returns → track survives |
| 6.3 | Head angle clamping | HeadController clamps to [-22°, 45°] |

---

## Phase 7 — Signal Integration (3 tests)

| # | Test | Assert |
|---|------|--------|
| 7.1 | Signal gate validation | `pgm-signal-gate.sh` handles invalid event types gracefully |
| 7.2 | Bridge endpoints | Health → 200, `/audio/play` and `/led` endpoints respond |
| 7.3 | Robot-control skill | SKILL.md exists with LED, speech, status, and "robot" trigger |

---

## Phase 8 — Agent Loop (3 tests)

| # | Test | Assert |
|---|------|--------|
| 8.1 | Config + worker limits | `load_config()` valid, max_workers=4, max_vector_workers=2 |
| 8.2 | GitHub CLI | `gh auth status` authenticated, `gh issue list` returns JSON |
| 8.3 | PR review hook | Agent definition or dispatch.py exists |

---

## Phase 9 — Event Bus (2 tests)

| # | Test | Assert |
|---|------|--------|
| 9.1 | Pub/sub + ordering | Bus creates, events delivered in publish order |
| 9.2 | SDK event forwarding | Cliff event → NUC bus `emergency_stop` event |

---

## Phase 10 — Services Health (2 tests)

| # | Test | Assert |
|---|------|--------|
| 10.1 | wire-pod | Service active, web UI (8080) → 200, chipper port 443 listening |
| 10.2 | OpenClaw gateway | `localhost:18889/health` → 200 |

---

## Phase 12 — Signal→Robot E2E (2 tests)

Tests the full Signal→OpenClaw→robot path via WebSocket (no actual Signal messages needed).

| # | Test | Assert |
|---|------|--------|
| 12.1 | Robot commands | "robot say hello", "robot status", "robot set eyes green" → all get non-empty responses |
| 12.2 | Signal notification | `pgm-signal-gate.sh board-status` completes without crash |

---

## Phase 13 — LiveKit Integration (3 tests)

| # | Test | Assert |
|---|------|--------|
| 13.1 | Credentials + token | `.env.livekit` loads, JWT token generated (>50 chars) |
| 13.2 | Room connect | Connect to `robot-cam` room succeeds |
| 13.3 | Bridge import | `livekit_bridge.py` imports without error |

---

## Summary

| Phase | Tests | Needs Robot | Needs Services |
|-------|-------|-------------|----------------|
| 0 — Unit tests | 5 | No | No |
| 1 — Preflight | 4 | Yes | No |
| 2 — Stationary HW | 1 (13 sub) | Yes | No |
| 3 — Inference | 6 | No | No |
| 4 — Voice | 3 | Yes (4.1) | No |
| 5 — Movement | 1 (10 sub) | Yes | No |
| 6 — Following | 3 | No (mocked) | No |
| 7 — Signal | 3 | No | Yes (bridge) |
| 8 — Agent loop | 3 | No | No (gh CLI) |
| 9 — Event bus | 2 | No | No |
| 10 — Services | 2 | No | Yes |
| 12 — Signal→Robot E2E | 2 | No | Yes (OpenClaw) |
| 13 — LiveKit | 3 | No | Yes (LiveKit Cloud) |
| **Total** | **38** | | |

## Architecture Tested

```
Signal → OpenClaw gateway → robot-control skill → curl → bridge → Vector gRPC
                                                           ↕
wire-pod → wake word → STT → OpenClaw hooks → response → say_text()
                                                           ↕
LiveKit Cloud ← phone camera    NUC inference (YOLO, face, Kalman) ← Vector camera
                                                           ↕
                                Event bus (SDK events + NUC bus) → follow planner → motors
```
