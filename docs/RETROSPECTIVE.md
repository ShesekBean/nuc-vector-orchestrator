# Vector Orchestrator — Project Retrospective

**Date:** 2026-03-14
**Duration:** 5 days (2026-03-10 to 2026-03-14)
**Commits:** 188
**Issues closed:** 76
**Lines of Python:** ~46,000
**Lessons learned entries:** 56

---

## Timeline

### Day 1 (Mar 10) — Foundation
**Commits:** 20

Repo setup and porting from R3. Initial commit, ported NUC orchestrator and OpenClaw to Vector platform, added full OpenClaw configuration reference. Set up Monarch Money skill, fitness skill with Oura/Strava/Withings integration, sleep coaching cron job, automatic token refresh. Renamed agent identity from Shon to Vector. Updated architecture docs for hybrid event system and OpenVINO inference. Mostly scaffolding and OpenClaw extensions — no robot code yet.

### Day 2 (Mar 11) — The Big Build
**Commits:** 92 | **Issues closed:** ~35

The single most productive day. Built virtually all core subsystems from scratch:

- **Infrastructure:** Event bus, OpenVINO setup, PGM auto-unstick, connection config, standalone test scripts
- **Controllers:** LED (eye color priority states), head servo, lift, motor (cliff-safe differential drive), sensor/touch/cliff events, battery monitor
- **Perception:** Camera streaming, YOLO person detection, Kalman filter tracker, face recognition (YuNet + SFace), scene description, obstacle detection
- **Audio:** Mic streaming via gRPC, wake word detection (SDK + openwakeword), voice bridge (STT via OpenClaw Talk Mode), speech output (say_text chunking), echo suppression
- **Intelligence:** Expression engine (face + LED + sound), follow planner (PD controller), visual SLAM, bridge server (HTTP-to-gRPC)
- **Testing:** Golden test rewrite for Vector, standalone test scripts per subsystem
- **Agent loop improvements:** Auto-rebase in merge gate, doc-only PR filtering, 61% wasted cycle fix

The agent loop dispatched workers in parallel — up to 4 simultaneous issue workers handling design, code, review, test, and merge autonomously. This is how 35 issues got closed in a single day.

### Day 3 (Mar 12) — Integration
**Commits:** 118 | **Issues closed:** ~30

Connected the day-2 components into working pipelines:

- **Robot control:** Ported OpenClaw robot-control skill (Signal → HTTP → gRPC), intercom (photo + voice to Signal)
- **Voice:** Voice command routing (wire-pod intents + OpenClaw natural language), message relay (voice-to-Signal)
- **LiveKit:** WebRTC session for remote see/hear/talk, mic audio streaming to room
- **Tracking:** Head tracking during person follow, follow pipeline status endpoint
- **Physical testing:** Ported Signal-based physical test framework with 9 test categories
- **Process management:** Supervisor with ordered startup (16 components), health monitoring, battery-aware reduction
- **Display fix:** Discovered Vector 2.0 screen is 160x80 not 184x96, began DisplayFaceImageRGB patching
- **Bug fixes:** CI fixes (unused imports, stale test mocks, protobuf import failures), SKILL.md drift across 3 copies

The high commit count (118) reflects both feature work and the large number of doc-check issues that the post-merge hook generated — each requiring a REPO_MAP.md update.

### Day 4 (Mar 13) — Advanced Features + Hardware Battles
**Commits:** 96

The day of hardest hardware problems and most ambitious features:

- **Display fix saga:** Three patch iterations for DisplayFaceImageRGB — heap corruption (non-static buffer), garbled images (wrong stride 184 vs 160), permanent eye animation kill (KeepFaceAlive bug). Final fix: static buffer + stride conversion in libcozmo_engine.so + control release/re-acquire for face restore
- **Mic audio saga:** Discovered ALSA doesn't work on Vector (Qualcomm ADSP bypasses kernel audio). Built vector-streamer native binary (wire-pod chipper tap), then rewrote to use SDK AudioFeed's signal_power field (which actually contains 15625Hz int16 PCM — misleading field name)
- **Follow pipeline v2:** Four iterations — Kalman runaway causing backward driving, wrong frame dimensions (800x600 not 640x360), switched to P-only with turn-first-then-drive + EMA direction reset
- **LiveKit two-way:** Full audio/video streaming with echo prevention (flag file + mic-to-vic-cloud mute)
- **Quiet mode:** Attempted SDK behavior control release + wire-pod cloud_intent. Ultimately reverted to OVERRIDE_BEHAVIORS after discovering 15s keepalive requirement
- **Porcupine wake word:** Two-process architecture with /data/ cache for writable HOME, NTP sync guard
- **Behavior mode API:** Battery percent estimation from LiPo voltage curve
- **Navigation system:** IMU fusion, A* path planning, waypoint management
- **Autonomous explorer:** Room discovery with Signal-based naming, auto-charge on low battery, voice feedback
- **Home Guardian:** Smart patrol with security alerts
- **Low-light improvements:** Multi-stage preprocessing, adaptive YOLO confidence, camera exposure control

### Day 5 (Mar 14) — Polish + Companion
**Commits:** 50

- **Companion behavior system:** Presence tracking, OpenClaw personality integration, engagement-adaptive throttling (check-in frequency based on interaction patterns)
- **Dead reckoning:** Odometry from motor commands, auto-save charger waypoint, resume after charge
- **Explorer improvements:** Drive off charger, override control on demand, seed start area, initial 360-degree scan
- **SDK connection fix:** OVERRIDE_BEHAVIORS_PRIORITY for reliable behavior control
- **Documentation and retrospective**

---

## What Worked Well

### Software Architecture

1. **Hybrid event bus** — Lightweight NUC pub/sub (~100 lines) plus SDK events. Avoided ROS2 overhead while keeping clean decoupling. Every component communicates through typed events. SDK handles hardware events (face, cliff, touch, wake_word, connection_lost); NUC bus handles AI-computed events (YOLO detections, follow state, STT results).

2. **Ordered supervisor startup** — 16 components start in dependency order (motor, sensor, camera, through to companion). Health monitoring restarts crashed components. Battery-aware functionality reduction disables non-essential subsystems when voltage drops. This pattern prevented the startup race conditions that plagued R3.

3. **Turn-first-then-drive** — Solved differential drive control cleanly. No arc trajectories, predictable movement. Simple but correct. The follow pipeline went through four iterations before landing on this — every attempt at combined turning and driving created unpredictable arcs.

4. **Position-only Kalman** — Tracking [cx, cy, vx, vy] with frozen bbox prevented the oscillation that plagued R3's full-state tracker. YOLO at 15fps on OpenVINO is fast enough that heavy prediction is unnecessary. Never predict bbox width/height since YOLO sizes vary naturally with pose changes.

5. **Echo suppression via blocking say_text()** — Trivial implementation: pause mic during TTS, flush buffer after. No complex acoustic echo cancellation needed. The SpeechOutput module wraps all chunks in a single TTS_PLAYING event pair, and the EchoSuppressor subscribes to those events.

6. **Cliff-safe motor control** — Single `_safe_drive` choke point with bitmask cliff checking (front-left=0x01, front-right=0x02, rear-left=0x04, rear-right=0x08). Emergency cliff reactions bypass the choke point to avoid deadlock, but all user-facing motor methods route through it.

7. **Agent loop automation** — Workers handled 35+ issues autonomously on day 2 alone. Full lifecycle (design, code, self-review, test, merge) with quality gates. Account rotation across 3 Claude accounts kept throughput high when quota limits hit. PGM auto-unstick parsing dependency sections and auto-requeueing was a force multiplier.

8. **OpenClaw as the brain** — Delegating personality, decision-making, and skill routing to OpenClaw was the right call. NUC code stays mechanical (detect, track, throttle). OpenClaw handles all intelligence. The companion behavior system sends context to OpenClaw and lets it generate personality-appropriate responses.

9. **Bridge server pattern** — aiohttp REST-to-gRPC translation gave OpenClaw and voice identical access to robot hardware. Adding new capabilities means adding a route. Simple, debuggable, and the same endpoints work from Signal, voice commands, and LiveKit sessions.

10. **Standalone-first testing** — Having standalone scripts per subsystem (test_camera.py, test_motors.py, test_mic.py, etc.) caught issues before integration. Each script is truly self-contained with its own connection config — no shared imports, even at the cost of duplication.

### Hardware Decisions

1. **All compute on NUC** — Vector's Snapdragon 212 is too weak for anything beyond basic sensor polling. Running everything on the NUC (i7-1360P) gave us 15fps YOLO, real-time face recognition, and Claude Vision API calls with no contention. WiFi latency (50-100ms) is acceptable because Vector moves at only 200mm/s.

2. **OpenVINO over CUDA** — No discrete GPU on the NUC, but OpenVINO on Intel CPU/iGPU gives 47ms per frame for YOLO11n. Good enough for a robot whose maximum speed means a 2Hz tracking loop is responsive enough.

3. **wire-pod** — Stable 24/7, handles all Vector cloud dependencies (authentication, pairing, intent engine, weather, knowledge graph). Community-maintained but rock-solid in practice. The chipper audio tap also turned out to be the viable path for mic audio capture.

4. **say_text() over OpenAI TTS** — Zero cost, no audio streaming complexity, works immediately. Sound quality is "robot-like" but that is actually charming for a companion robot. Decision made on day 2 and never revisited.

### Process

1. **GitHub Issues as coordination** — Clean separation between orchestrator (creates issues) and workers (close them). Labels drive dispatch: `assigned:worker` for ready issues, `component:vector` for robot code, `stuck` for blocked issues. Simple and effective.

2. **PGM auto-unstick** — Parsing `## Dependencies` sections in issue bodies and auto-requeueing when all dependencies close. No manual label management needed. This removed a major bottleneck where issues sat in `stuck` state long after their blockers were resolved.

3. **Lessons-learned JSONL** — Every issue captures what was learned. 56 entries in 5 days. The structured format (issue number, date, category, lesson, fix) makes them searchable and prevents repeating mistakes.

4. **MD file consistency rule** — Cross-checking docs on every change caught many drift issues. The post-merge doc-check hook was noisy (generated dozens of REPO_MAP.md update issues) but effective at keeping documentation accurate.

---

## What Didn't Work / Was Hard

### Hardware Challenges

1. **Vector's display is 160x80, not 184x96** — SDK documentation is wrong for Vector 2.0 (Xray). Took three patch iterations to fix DisplayFaceImageRGB: heap corruption from non-static buffer allocation, garbled images from wrong stride (184 vs 160), and permanent eye animation kill from the KeepFaceAlive bug. Final fix required patching libcozmo_engine.so with static buffer plus stride 184-to-160 conversion, and implementing a control release/re-acquire cycle for face restore.

2. **Mic audio is NOT accessible via ALSA** — Vector's 4 DMICs go through Qualcomm ADSP (FastRPC), completely bypassing the Linux kernel audio subsystem. ALSA arecord returns white noise (TERT_MI2S) or I/O error (QUAT_MI2S). Three approaches attempted: (a) wire-pod chipper tap intercepts beamformed audio after wake word, (b) native vector-streamer binary with DGRAM proxy, (c) SDK AudioFeed's signal_power field which actually contains 15625Hz int16 PCM despite the misleading field name. All three work; the SDK approach ended up simplest.

3. **Camera is 800x600, not 640x360** — SDK docs say 640x360 but actual frames are 800x600. Caused wrong frame dimensions in the follow pipeline until corrected, leading to incorrect center-of-frame calculations and erratic following behavior.

4. **KeepFaceAlive bug** — After displaying a custom image, face animations (eyes, blinks, expressions) are permanently disabled for the session. The only working fix is releasing and re-acquiring SDK behavior control to force a full animation pipeline reset. This is undocumented.

5. **wire-pod RTS v7** — wire-pod's BLE setup doesn't handle RTS protocol v7 Vectors. Had to use the anki.bot web setup interface or a local mirror instead of the standard BLE pairing flow.

6. **vic-cloud hardcodes 184x96** — The Go gateway binary always sends 30 chunks at 184-pixel stride. Cannot change without recompiling vic-cloud. Solution was to handle the stride conversion downstream in vic-engine (libcozmo_engine.so patch).

### Software Challenges

1. **Follow pipeline iterations** — V1 used PD controller with Kalman smoothing; Kalman state runaway caused backward driving when predictions diverged from measurements. V2 added turn-first-then-drive but still had wrong frame dimensions. V3 fixed dimensions but still oscillated. V4 switched to P-only with EMA direction reset. Four iterations over two days to get stable following — each fix revealed the next issue.

2. **QuietMode complexity** — Needed to release SDK behavior control, send quiet intent via wire-pod cloud_intent API, and run a 15-second keepalive thread because voice interactions deactivate the intent. Porcupine wake word detection added another process. Eventually moved to a feature branch and reverted to OVERRIDE_BEHAVIORS mode because the keepalive was fragile and interfered with other subsystems.

3. **LiveKit echo loops** — Mic audio streams to LiveKit, plays on remote device, remote audio comes back to Vector speaker, mic picks it up again. Fixed with a flag file during calls plus mic-to-vic-cloud mute. Required three separate fixes: flag file detection, voice proxy suppression, and empty-room timeout tuning.

4. **CI/test environment** — anki_vector SDK not installable in CI (requires Vector hardware for authentication). Required elaborate sys.modules stubs in conftest.py. Every new module that imported the SDK needed lazy imports (inside functions, not at module level) and corresponding mock adjustments. OpenCV 4.13.0 also introduced a breaking change to fastNlMeansDenoisingColored argument handling.

5. **SKILL.md drift** — Three copies of robot-control SKILL.md (apps/openclaw/, infra/openclaw/, ~/.openclaw/workspace/skills/) constantly drifted. Every endpoint change had to touch all three files plus OPENCLAW-CONFIG.md. The review hook caught inconsistencies but the fix was always tedious manual synchronization.

6. **Porcupine activation limits** — Free tier shared quota across all Picovoice users. Fixed with HOME=/data/ cache on Vector for a writable activation directory and NTP time sync requirement before initialization. Still fragile due to shared quota.

### Agent Loop / Workflow Issues

1. **61% dispatch cycles wasted** — Rebases and pre-existing CI failures consumed most worker time before the auto-rebase fix on day 2. Workers would pick up an issue, write code, then fail at merge due to conflicts from parallel workers. Auto-rebase in the merge gate and doc-only PR filtering were the highest-impact workflow fixes.

2. **Doc-check issue spam** — Post-merge hooks created dozens of REPO_MAP.md update issues (issues 48, 49, 52, 54, 55, 64, 69, 73, 79, 81, 86, 93, 96, 103, 108, 113, 116, 120, 121, 124, 127, 138, 140 — 23 doc-check issues out of 76 total). Useful for catching drift but noisy. Each one consumed a full worker dispatch cycle for a one-line REPO_MAP.md change.

3. **Constructor signature mismatches** — Golden test rewrites required exact constructor signatures verified against source. Positional argument order differs across modules (PersonDetector takes event_bus as keyword, KalmanTracker takes no bus, FollowPlanner takes motor/head/bus positionally). Module-level constants vs class-level constants also tripped up automated test generation.

---

## Hardware Lessons Learned

1. **Never trust SDK documentation for hardware specs** — verify camera resolution, display size, and mic access method by testing on actual hardware. Every spec we checked was wrong for Vector 2.0.

2. **Vector 2.0 (Xray) is meaningfully different from 1.0 (Victor)** — different screen resolution (160x80 vs 184x96), different gamma, different chunk count in display pipeline. Code that works on 1.0 may not work on 2.0.

3. **ADSP audio path is proprietary** — Qualcomm's ADSP subsystem completely bypasses Linux ALSA. No standard audio capture tools work. The only viable paths are SDK AudioFeed, wire-pod chipper tap, or custom ADSP binaries.

4. **Static buffers prevent heap corruption on embedded** — the repeated alloc/dealloc pattern crashes on Vector's limited heap. Static allocation in the DisplayFaceImageRGB patch solved the heap corruption that three other approaches failed to fix.

5. **SDK AudioFeed.signal_power contains PCM audio at 15625Hz** — this is undocumented and the field name is misleading. Resample to 16000Hz via linear interpolation for standard audio processing compatibility.

6. **WiFi latency (50-100ms) is acceptable for slow robots** — Vector moves at 200mm/s max, so a 2Hz tracking loop is responsive enough. No need for on-robot inference or low-latency communication protocols.

7. **Differential drive means turn-first-then-drive** — do not try to combine turning and driving into simultaneous wheel speed differentials. It creates unpredictable arcs. Sequential turn-then-drive is simpler and more predictable.

8. **Cliff sensors are bitmask, not boolean** — front-left=0x01, front-right=0x02, rear-left=0x04, rear-right=0x08. Check direction-specific bits for smart recovery (back away from the specific edge, not just stop).

9. **Battery percent must be estimated** — SDK only gives voltage (3.5-4.2V) and a coarse level enum (0-3). LiPo voltage curve mapping needed for usable percentage. Voltage sags under load so hysteresis is important.

10. **KeepFaceAlive is a permanent flag** — once cleared by image display, the only way to restore face animations is a full behavior control release and re-acquire cycle. No API to set it back directly.

---

## Software Lessons Learned

1. **Lazy imports for CI compatibility** — anki_vector, cv2, and numpy must be imported inside functions, not at module level, for test stubs to work in CI where these packages are not installable.

2. **sys.modules stubs in conftest** — the only reliable way to mock SDK imports in CI. Module-level `patch.dict(sys.modules, ...)` in conftest.py, not per-test patches.

3. **Position-only Kalman is better than full-state Kalman** — predicting bbox dimensions causes oscillation because YOLO bbox sizes naturally vary with pose. Just track center position [cx, cy, vx, vy] and freeze bbox at the last measurement.

4. **Event ordering matters for safety** — emit emergency_stop before diagnostic events like cliff_triggered to avoid delaying motor stop while the bus processes informational events.

5. **Single choke point for motor safety** — all user commands through `_safe_drive()`, but emergency reactions bypass it to avoid deadlock. Never add a second path for "normal" motor commands.

6. **Blocking say_text() is a feature, not a bug** — makes echo suppression trivial by providing a natural synchronization point for mic muting.

7. **P controller is enough at 15fps** — D term adds noise at this update rate with no measurable benefit. The follow pipeline went through PD, PID back to P-only.

8. **Three copies of SKILL.md equals three sources of drift** — need a sync mechanism or single source of truth with symlinks. Manual synchronization does not scale.

9. **aiohttp needs pytest-aiohttp** — and standalone mock factories work better than shared async fixtures. Each test creates its own mock connection via a factory function.

10. **Dead reckoning degrades fast** — visual SLAM is essential even for rough position estimates. Pure odometry from motor commands drifts significantly within a single room traversal.

11. **Engagement-adaptive throttling** — companion check-ins based on interaction frequency prevents annoyance. A robot that greets you every time you walk past gets old fast.

12. **OpenClaw WebSocket protocol** — challenge nonce, connect, chat.send, collect events until state=final. client_id must be "gateway-client". Understanding this protocol was necessary for voice bridge integration.

---

## What We'd Do Differently

1. **Single SKILL.md source** — automate sync or use symlinks instead of maintaining 3 copies. Every endpoint change required touching 3-4 files, and drift was caught by review hooks after the fact rather than prevented.

2. **Start with Porcupine, not openwakeword** — Vector SDK wake word ("Hey Vector") is too limited for custom triggers, and openwakeword threshold tuning was wasted work. Porcupine's pre-trained models work better out of the box.

3. **Build golden tests in parallel with features** — not after. The golden test rewrite on day 3 found constructor signature issues that would have been caught immediately if tests were written alongside the code.

4. **Camera resolution verification first** — testing actual frame dimensions before writing the follow pipeline would have avoided two iterations of fixes (640x360 assumption vs 800x600 reality).

5. **Auto-rebase from day 1** — 61% wasted dispatch cycles in the first day could have been avoided. The auto-rebase fix was the single highest-impact workflow improvement.

6. **Less aggressive doc-check hooks** — batch REPO_MAP.md updates instead of one issue per missing entry. 23 out of 76 closed issues (30%) were doc-check issues that each consumed a full worker dispatch cycle for a one-line change.

7. **Verify hardware specs empirically before coding** — display resolution, camera resolution, mic access method, and audio field contents were all wrong in documentation. A single afternoon of hardware verification would have saved days of debugging.

8. **Feature branch for experimental subsystems** — QuietMode should have been on a feature branch from the start instead of being built on main and then reverted. The revert touched many files and created unnecessary churn.

---

## Current State (2026-03-14)

### Working End-to-End

- **Perception:** Person detection (YOLO11n, 15fps OpenVINO), face recognition (YuNet + SFace), Kalman tracking, low-light adaptive preprocessing
- **Following:** Person following with head tracking, turn-first-then-drive, obstacle avoidance (camera-based, no LiDAR), cliff-safe motor control
- **Voice:** Wake word (Porcupine) to STT (wire-pod Vosk) to command routing (wire-pod intents + OpenClaw natural language via openclaw-voice-proxy) to TTS (say_text) with echo suppression
- **Communication:** Signal to OpenClaw to robot control (all commands), voice-to-Signal relay, intercom (photo + text to Signal)
- **Video calls:** LiveKit WebRTC with camera + mic + speaker + display, echo prevention, empty-room timeout
- **Companion:** Presence tracking, greeting/check-in/goodnight behaviors, touch responses, engagement-adaptive throttling, OpenClaw personality integration
- **Navigation:** Autonomous exploration with room naming via Signal, indoor navigation (A* path planning, waypoint management), dead reckoning with visual SLAM fusion
- **Security:** Smart patrol with security alerts, home guardian mode
- **Expressions:** Multi-modal expression engine (face display + LED eye color + sound coordination)
- **Infrastructure:** Battery monitoring with automatic functionality reduction, ordered supervisor startup (16 components), process health monitoring, auto-charge on low battery

### Not Yet Working

- Cube interaction (issue #40 — lift + object detection integration)
- Oura sleep data integration for companion night mode (time-based fallback is active)
- Wake word during SDK quiet mode (needs QuietMode feature branch completion + new Picovoice key)
- Multi-Vector support (single robot only, architecture supports it but untested)

### By the Numbers

| Metric | Value |
|--------|-------|
| Total commits | 188 |
| Issues closed | 76 |
| Issues that were doc-checks | 23 (30%) |
| Lines of Python | ~46,000 |
| Lessons learned entries | 56 |
| Subsystems built | 16 (supervisor-managed) |
| Follow pipeline iterations | 4 |
| Display patch iterations | 3 |
| Mic audio approaches tried | 3 |
| SKILL.md copies to maintain | 3 |
| SDK documentation specs that were wrong | 3 (camera, display, mic) |
| Peak commits in one day | 118 (day 3) |
| Peak issues closed in one day | ~35 (day 2) |
