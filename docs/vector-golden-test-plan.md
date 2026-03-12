# Vector Golden Test Plan

Comprehensive end-to-end test covering all project subsystems in ~3.5 minutes.

**Goal:** Maximum coverage in minimum time. Tests are ordered so that failures in early phases abort before wasting time on dependent phases.

---

## Phase 0 — NUC Unit Tests (~20s)

No robot needed. Run first to catch regressions before touching hardware.

```bash
python3 -m pytest tests/ -x --timeout=20
```

| # | Test | Assert |
|---|------|--------|
| 0.1 | Agent loop config loads | `Config()` returns valid object, all paths exist |
| 0.2 | LLM config parses | `parse_llm_config()` returns correct provider/models |
| 0.3 | Dispatch label matching | Issues with `assigned:worker` get picked up |
| 0.4 | PGM dependency parser | `## Dependencies` section parsed, `#N` refs extracted |
| 0.5 | Inbox command routing | `#approve`, `#status`, `#reject` route to correct handlers |
| 0.6 | Signal gate script | `scripts/pgm-signal-gate.sh` validates event types |
| 0.7 | Worker prompt injection | `component:vector` issues get gRPC context block |
| 0.8 | Worktree path generation | Vector → `/tmp/vector-worker-issue-{N}`, NUC → `/tmp/nuc-worker-issue-{N}` |

---

## Phase 1 — Preflight: Robot Connectivity + Battery Gate (~12s)

**Gate:** If this phase fails, skip Phases 2, 5, 6. NUC-only phases (3, 4, 7, 8, 9) still run.

| # | Test | Command | Assert |
|---|------|---------|--------|
| 1.1 | Vector SDK connects | `robot.connect()` | No exception, `robot.status` populated |
| 1.2 | Battery voltage | `robot.get_battery_state()` | Voltage > 3.6V (safe for motors) |
| 1.3 | gRPC round-trip latency | Timed `robot.get_battery_state()` | < 500ms |
| 1.4 | Request control | `robot.conn.request_control()` | Control granted |
| 1.5 | Release control | Release + re-request | Clean handoff, no errors |

---

## Phase 2 — Stationary Hardware (~35s)

Robot stays still. Tests all non-movement hardware via gRPC.

| # | Test | Command | Assert |
|---|------|---------|--------|
| 2.1 | Camera single frame | `robot.camera.capture_single_image()` | Returns image, shape = (360, 640) or similar |
| 2.2 | Camera feed stream | `CameraFeed` gRPC, capture 5 frames | 5 frames received, no duplicates (timestamp check) |
| 2.3 | Head angle min | `robot.behavior.set_head_angle(degrees(-22))` | Head moves to min position |
| 2.4 | Head angle max | `robot.behavior.set_head_angle(degrees(45))` | Head moves to max position |
| 2.5 | Head angle neutral | `robot.behavior.set_head_angle(degrees(10))` | Returns to neutral |
| 2.6 | Lift up | `robot.behavior.set_lift_height(1.0)` | Lift rises |
| 2.7 | Lift down | `robot.behavior.set_lift_height(0.0)` | Lift lowers |
| 2.8 | LED solid color | `robot.behavior.set_backpack_lights(Color(rgb=[255,0,0]))` | Backpack turns red |
| 2.9 | LED off | `set_backpack_lights(off)` | Backpack dark |
| 2.10 | Display image | `robot.behavior.display_oled_face_image(img_184x96)` | 184x96 image displayed on OLED |
| 2.11 | TTS say_text | `robot.behavior.say_text("test")` | Audio plays from speaker |
| 2.12 | Touch sensor read | `robot.touch.last_sensor_reading` | Returns valid touch state (bool) |
| 2.13 | Cliff sensors read | `robot.sensors` cliff data | 4 cliff sensor values returned |
| 2.14 | Accelerometer read | `robot.accel` | 3-axis values, non-zero |
| 2.15 | Gyroscope read | `robot.gyro` | 3-axis values returned |

---

## Phase 3 — NUC Inference Pipeline (~25s)

No robot needed (uses saved test images). Tests all CV/ML on NUC.

| # | Test | Assert |
|---|------|--------|
| 3.1 | YOLO model loads | OpenVINO IR model loads without error |
| 3.2 | YOLO person detect | Test image with person → bbox with class=person, confidence > 0.5 |
| 3.3 | YOLO no-person | Test image with no person → no person detections |
| 3.4 | Face detection (YuNet) | Test image with face → face bbox returned |
| 3.5 | Face recognition (SFace) | Two images of same person → cosine similarity > threshold |
| 3.6 | Face different people | Two different people → cosine similarity < threshold |
| 3.7 | Kalman tracker init | Detection → tracker creates track with ID |
| 3.8 | Kalman tracker update | 5 sequential detections → smooth trajectory, no ID switch |
| 3.9 | Kalman tracker timeout | No detections for N frames → track dropped |
| 3.10 | Scene description | Camera frame + Claude Vision API → text description returned |
| 3.11 | Detection → event bus | YOLO detection fires `person_detected` event |

---

## Phase 4 — Voice Pipeline (~30s)

Tests audio streaming and wake word detection on NUC.

| # | Test | Command | Assert |
|---|------|---------|--------|
| 4.1 | Audio stream connects | `AudioFeed` gRPC from Vector | Raw audio bytes received |
| 4.2 | Audio format | Check sample rate/channels | Matches expected format (16kHz mono) |
| 4.3 | Wake word model loads | openwakeword init | Model loaded, no errors |
| 4.4 | Wake word positive | Play wake word audio → detector | Detection event fired |
| 4.5 | Wake word negative | Play non-wake-word audio | No false trigger |
| 4.6 | STT via OpenClaw | Talk Mode gpt-4o-transcribe | Transcript returned for test audio |
| 4.7 | TTS say_text round-trip | `say_text("hello")` → mic capture | Audio detected on mic (energy > threshold) |

---

## Phase 5 — Movement + Safety (~35s)

**Requires:** Phase 1 passed. Robot MUST be on a safe surface (floor or large table with margins).

| # | Test | Command | Assert |
|---|------|---------|--------|
| 5.1 | Drive forward | `DriveStraight(50, 100)` | Robot moves ~50mm forward |
| 5.2 | Drive backward | `DriveStraight(-50, 100)` | Robot moves ~50mm backward |
| 5.3 | Turn right 90° | `TurnInPlace(90, 100, 100, 2)` | Robot turns ~90° clockwise |
| 5.4 | Turn left 90° | `TurnInPlace(-90, 100, 100, 2)` | Robot turns ~90° counter-clockwise |
| 5.5 | Cliff sensor pre-check | Read cliff sensors before move | All 4 sensors report safe |
| 5.6 | Cliff safety wrapper | `safe_drive()` with cliff check | Pre-move cliff validation runs |
| 5.7 | Emergency stop | `DriveWheels(0,0,0,0)` mid-motion | Robot stops immediately |
| 5.8 | Speed clamp | Request 500mm/s | Clamped to max 200mm/s |
| 5.9 | Head tracks during move | Move forward + head stays at set angle | Head angle stable |
| 5.10 | Post-move position | Encoders/IMU | Position changed from start |

---

## Phase 6 — Person Following Integration (~40s)

**Requires:** Phases 1, 3, 5 passed. Needs a person visible to camera.

| # | Test | Assert |
|---|------|--------|
| 6.1 | Person acquisition | YOLO detects person in camera frame |
| 6.2 | Tracking lock | Kalman tracker assigns consistent ID across 10 frames |
| 6.3 | Head tracking | Head servo tracks person as they move laterally |
| 6.4 | Follow engage | Follow mode starts, robot turns toward person |
| 6.5 | Follow disengage | Person leaves FOV → robot enters 360° search |
| 6.6 | 360° search — head sweep | Head pans -22° to 45° looking for person |
| 6.7 | 360° search — body rotation | After head sweep fails, robot rotates 90° × 3 |
| 6.8 | Face detection (seated) | Seated person with visible face → face detection triggers follow |
| 6.9 | Target re-acquisition | Person returns to FOV → follow resumes with same track ID |
| 6.10 | Follow stop command | Stop following → robot stops, motors idle |

---

## Phase 7 — Signal Integration (~18s)

Tests OpenClaw → bridge → robot path. No physical robot motion needed.

| # | Test | Assert |
|---|------|--------|
| 7.1 | OpenClaw gateway health | `curl localhost:18889/health` → 200 |
| 7.2 | Signal send (Signal gate) | `pgm-signal-gate.sh test 0 "test"` → message sent |
| 7.3 | Bridge health endpoint | `curl localhost:8081/health` → 200 + battery info |
| 7.4 | Bridge → say_text | `POST /say {"text":"test"}` → robot speaks |
| 7.5 | Bridge → LED | `POST /led {"r":0,"g":255,"b":0}` → green LED |
| 7.6 | PGM Signal prefixes | PGM messages start with `📊 PGM:` |
| 7.7 | OpenClaw skill loaded | robot-control skill present in `~/.openclaw/workspace/skills/robot-control/SKILL.md` |
| 7.8 | E2E Signal → robot LED | Send "robot led red" via Signal → OpenClaw agent activates skill → curl fires → robot eye color turns red |
| 7.9 | E2E Signal → robot speak | Send "robot say hello" via Signal → OpenClaw agent → `say_text("hello")` → audio plays |
| 7.10 | E2E Signal → robot status | Send "robot status" via Signal → OpenClaw agent → `GET /health` → battery info returned in Signal reply |
| 7.11 | Skill trigger guard | Send message WITHOUT "robot" keyword → skill does NOT activate, normal chat response |

---

## Phase 8 — Agent Loop (~12s)

Tests dispatch machinery. No workers actually launched.

| # | Test | Assert |
|---|------|--------|
| 8.1 | Config loads | `load_config()` returns valid Config |
| 8.2 | GitHub CLI auth | `gh auth status` → authenticated |
| 8.3 | Issue list | `gh issue list` → valid JSON |
| 8.4 | Label filtering | `assigned:worker` filter returns correct issues |
| 8.5 | Worker slot counting | max_workers=4, max_vector_workers=2 respected |
| 8.6 | Worktree creation | Create + cleanup test worktree |
| 8.7 | PR review hook exists | Review hook script present and executable |

---

## Phase 9 — Event Bus Integration (~8s)

Tests the hybrid event system (SDK events + NUC bus).

| # | Test | Assert |
|---|------|--------|
| 9.1 | NUC event bus init | Event bus creates without error |
| 9.2 | Event publish/subscribe | Publish test event → subscriber receives it |
| 9.3 | SDK event forwarding | SDK `object_observed` → NUC bus event |
| 9.4 | Event type registry | All expected event types registered |
| 9.5 | Event ordering | Events delivered in publish order |

---

## Execution Order

```
Phase 0 (NUC unit tests)
    ↓ pass
Phase 1 (preflight) ──fail──→ skip 2,5,6 → continue 3,4,7,8,9
    ↓ pass
Phase 2 (stationary HW)     Phase 3 (inference)    Phase 4 (voice)
    ↓                            ↓                      ↓
Phase 5 (movement)
    ↓ pass
Phase 6 (following)          Phase 7 (Signal)       Phase 8 (agent loop)
                             Phase 9 (event bus)
```

**Parallel where possible:** Phases 2/3/4 can run concurrently. Phases 7/8/9 can run concurrently.

---

## Running the Full Suite

```bash
# Full run (needs robot + person for phases 1-6)
python3 -m pytest tests/golden/ -v --timeout=300

# NUC-only (no robot needed)
python3 -m pytest tests/golden/ -v --timeout=120 -k "phase0 or phase3 or phase4 or phase7 or phase8 or phase9"

# Hardware-only (robot connected, no person needed)
python3 -m pytest tests/golden/ -v --timeout=180 -k "phase0 or phase1 or phase2 or phase5"
```

---

## Total: ~4 minutes, 89 assertions

| Phase | Tests | Time | Needs Robot | Needs Person |
|-------|-------|------|-------------|--------------|
| 0 — Unit tests | 8 | 20s | No | No |
| 1 — Preflight | 5 | 12s | Yes | No |
| 2 — Stationary HW | 15 | 35s | Yes | No |
| 3 — Inference | 11 | 25s | No | No |
| 4 — Voice | 7 | 30s | Yes (mic) | No |
| 5 — Movement | 10 | 35s | Yes | No |
| 6 — Following | 10 | 40s | Yes | Yes |
| 7 — Signal + OpenClaw E2E | 11 | 30s | Yes | No |
| 8 — Agent loop | 7 | 12s | No | No |
| 9 — Event bus | 5 | 8s | No | No |
| **Total** | **89** | **~247s** | | |

*Phase 7 E2E tests send actual Signal messages and verify robot response — needs robot powered on.
