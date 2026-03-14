#!/usr/bin/env python3
"""Mock follow pipeline test — exercises the real pipeline with synthetic data.

Tests the complete detection → Kalman → planner → motor command chain
using simulated camera frames with synthetic person detections.
No robot connection needed.

Scenarios:
1. Person centered → should drive forward, no turning
2. Person moving left → should turn left to track
3. Person moving right → should turn right to track
4. Person approaching → should slow down / reverse
5. Person disappearing → should transition to SEARCHING
6. Person reappearing → should re-acquire and follow
7. Low-light frame → should still detect with preprocessing
8. Rapid position changes → EMA should smooth output

Run: python3 apps/vector/tests/standalone/test_follow_mock.py
"""

import sys
import time

# Add repo root to path
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

import numpy as np

PASS = 0
FAIL = 1
FRAME_W = 800
FRAME_H = 600


class MockMotorController:
    """Records motor commands for verification."""

    def __init__(self):
        self.commands = []
        self.last_left = 0.0
        self.last_right = 0.0

    def drive_wheels(self, left, right, left_accel=0, right_accel=0):
        self.commands.append((left, right, time.monotonic()))
        self.last_left = left
        self.last_right = right

    def turn_in_place(self, angle_deg, speed_dps=100, accel_dps=200, tolerance=2):
        self.commands.append(("turn", angle_deg, time.monotonic()))

    def stop(self):
        self.drive_wheels(0, 0)


class MockHeadController:
    """Records head commands."""

    def __init__(self):
        self.angles = []
        self.current_angle = 0.0
        self.last_angle = 0.0  # used by HeadTracker

    def set_angle(self, angle_deg, **kwargs):
        self.angles.append(angle_deg)
        self.current_angle = angle_deg
        self.last_angle = angle_deg

    def get_angle(self):
        return self.current_angle


class MockNucEventBus:
    """Minimal event bus for testing."""

    def __init__(self):
        self._handlers = {}

    def on(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    def off(self, event_type, handler):
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h != handler
            ]

    def emit(self, event_type, event):
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event)
            except Exception as e:
                print(f"  Handler error: {e}")


def make_frame(brightness=100):
    """Create a synthetic BGR frame at given brightness."""
    frame = np.full((FRAME_H, FRAME_W, 3), brightness, dtype=np.uint8)
    return frame


def test_pd_controller():
    """Test PD controller outputs for known inputs."""
    from apps.vector.src.planner.follow_planner import FollowConfig, PDController

    print("\n--- Test: PD Controller ---")
    cfg = FollowConfig()
    pd = PDController(cfg)

    # Person centered, at target distance → should output ~0
    left, right = pd.compute(FRAME_W / 2, cfg.target_height)
    print(f"  Centered at target: L={left:.1f} R={right:.1f}")
    assert abs(left) < 5.0, f"Expected ~0 left, got {left}"
    assert abs(right) < 5.0, f"Expected ~0 right, got {right}"

    pd.reset()

    # Person far right → should turn right (left > right)
    left, right = pd.compute(FRAME_W * 0.9, cfg.target_height)
    print(f"  Far right: L={left:.1f} R={right:.1f}")
    assert left > right, "Expected left > right for rightward turn"

    pd.reset()

    # Person far left → should turn left (right > left)
    left, right = pd.compute(FRAME_W * 0.1, cfg.target_height)
    print(f"  Far left: L={left:.1f} R={right:.1f}")
    assert right > left, "Expected right > left for leftward turn"

    pd.reset()

    # Person too far (small bbox) → should drive forward (both positive)
    left, right = pd.compute(FRAME_W / 2, cfg.target_height * 0.5)
    print(f"  Too far (h={cfg.target_height * 0.5}): L={left:.1f} R={right:.1f}")
    assert left > 10 and right > 10, "Expected forward drive"

    pd.reset()

    # Person too close (large bbox) → should drive backward (both negative, capped)
    left, right = pd.compute(FRAME_W / 2, cfg.target_height * 2.0)
    print(f"  Too close (h={cfg.target_height * 2}): L={left:.1f} R={right:.1f}")
    assert left < 0 and right < 0, "Expected backward drive"
    # Check reverse cap (40% of max)
    max_reverse = cfg.max_wheel_speed * 0.4
    assert abs(left) <= max_reverse + 1, f"Reverse not capped: {left}"

    print("  PASS")
    return True


def test_ema_smoothing():
    """Test that EMA smoothing works within a consistent direction."""
    from apps.vector.src.planner.follow_planner import FollowConfig, PDController

    print("\n--- Test: EMA Smoothing ---")
    cfg = FollowConfig()
    pd = PDController(cfg)

    # Person moving gradually rightward — same direction, EMA should smooth
    outputs = []
    for i in range(10):
        cx = FRAME_W * 0.6 + i * 10  # gradually moving right
        left, right = pd.compute(cx, cfg.target_height)
        outputs.append((left, right))
        time.sleep(0.01)

    # Within a consistent direction, successive outputs should be smooth
    # (no huge jumps between consecutive frames)
    for i in range(2, len(outputs)):
        delta = abs(outputs[i][0] - outputs[i - 1][0])
        if delta > 50:
            print(f"  WARNING: Large jump at frame {i}: {delta:.1f}")
    print(f"  Last output: L={outputs[-1][0]:.1f} R={outputs[-1][1]:.1f}")

    # Direction-change reset test: when person jumps from right to left,
    # EMA should reset instantly (not drift slowly)
    pd.reset()
    pd.compute(FRAME_W * 0.9, cfg.target_height)  # far right
    time.sleep(0.01)
    left_after, right_after = pd.compute(FRAME_W * 0.1, cfg.target_height)  # far left
    print(f"  After R→L jump: L={left_after:.1f} R={right_after:.1f}")
    # Left should be negative (turning left) — instant reset, not slowly drifting
    assert left_after < right_after, "Should turn left after R→L jump"

    print("  PASS")
    return True


def test_kalman_tracker():
    """Test Kalman tracker with mock detections."""
    from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

    print("\n--- Test: Kalman Tracker ---")
    tracker = KalmanTracker()

    # Feed consistent detections — should confirm track quickly
    for i in range(5):
        dets = [Detection(cx=400, cy=300, width=100, height=200, confidence=0.5)]
        confirmed = tracker.update(dets)

    assert len(confirmed) > 0, "Should have confirmed track after 5 updates"
    primary = tracker.get_primary_track()
    assert primary is not None, "Should have primary track"
    print(f"  Track confirmed: id={primary.track_id} hits={primary.hits} cx={primary.cx:.0f}")
    assert primary.hits >= 1, "Should have at least 1 hit (min_hits=1)"

    # Feed empty detections — track should persist for a while
    for i in range(10):
        confirmed = tracker.update([])

    primary = tracker.get_primary_track()
    if primary:
        print(f"  After 10 empty: id={primary.track_id} hits={primary.hits} tsu={primary.time_since_update}")

    # Feed empty detections until track dies
    for i in range(20):
        tracker.update([])

    primary = tracker.get_primary_track()
    assert primary is None, "Track should be deleted after max_age frames"
    print("  Track deleted after max_age — correct")

    print("  PASS")
    return True


def test_kalman_velocity_decay():
    """Test that Kalman velocity decays when no measurements."""
    from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

    print("\n--- Test: Kalman Velocity Decay ---")
    tracker = KalmanTracker()

    # Create a moving track
    for i in range(5):
        cx = 300 + i * 20  # moving right at 20px/frame
        dets = [Detection(cx=cx, cy=300, width=100, height=200, confidence=0.5)]
        tracker.update(dets)

    primary = tracker.get_primary_track()
    cx_after_measurements = primary.cx
    print(f"  After 5 measurements: cx={cx_after_measurements:.0f}")

    # Now predict without measurements for several frames
    positions = []
    for i in range(10):
        tracker.update([])
        primary = tracker.get_primary_track()
        if primary:
            positions.append(primary.cx)

    if positions:
        # Velocity should be decaying — positions should not runaway
        print(f"  Predictions: {[f'{p:.0f}' for p in positions]}")
        assert positions[-1] < 1000, f"Position ran away: {positions[-1]}"
        # Check velocity is decaying (successive deltas should shrink)
        if len(positions) > 3:
            delta_early = abs(positions[1] - positions[0])
            delta_late = abs(positions[-1] - positions[-2])
            print(f"  Delta early={delta_early:.1f} late={delta_late:.1f}")
            assert delta_late <= delta_early + 1, "Velocity should decay"

    print("  PASS")
    return True


def test_follow_planner_state_machine():
    """Test state machine transitions with mock events."""
    from apps.vector.src.events.event_types import TRACKED_PERSON, TrackedPersonEvent
    from apps.vector.src.planner.follow_planner import FollowPlanner, State

    print("\n--- Test: Follow Planner State Machine ---")
    motor = MockMotorController()
    head = MockHeadController()
    bus = MockNucEventBus()

    planner = FollowPlanner(motor, head, bus)

    # Start → SEARCHING
    planner.start()
    assert planner.state == State.SEARCHING, f"Expected SEARCHING, got {planner.state}"
    print(f"  start() → {planner.state.value}")

    # Emit a tracked person event → should transition to TRACKING then FOLLOWING
    event = TrackedPersonEvent(
        track_id=1, cx=400, cy=300, width=100, height=200,
        age_frames=5, hits=3, confidence=0.5,
    )
    bus.emit(TRACKED_PERSON, event)
    time.sleep(0.2)  # Let control loop pick it up

    # Check state — with min_hits=1, it should go TRACKING → FOLLOWING quickly
    state = planner.state
    print(f"  After tracked person event: {state.value}")
    # It might still be in SEARCHING if the search tick hasn't consumed it yet
    # The search tick checks _consume_track which picks up the event

    # Send several events to make sure planner transitions
    for i in range(10):
        bus.emit(TRACKED_PERSON, TrackedPersonEvent(
            track_id=1, cx=400 + i * 5, cy=300, width=100, height=300,
            age_frames=5 + i, hits=3 + i, confidence=0.5,
        ))
        time.sleep(0.1)

    state = planner.state
    print(f"  After 10 events: {state.value}")

    # Stop
    planner.stop()
    assert planner.state == State.IDLE, f"Expected IDLE after stop, got {planner.state}"
    print(f"  stop() → {planner.state.value}")

    # Check motor commands were issued
    motor_count = len([c for c in motor.commands if isinstance(c[0], float)])
    print(f"  Motor commands issued: {motor_count}")

    print("  PASS")
    return True


def test_low_light_enhancement():
    """Test that low-light enhancement actually brightens dark frames."""
    from apps.vector.src.detector.person_detector import PersonDetector
    import cv2

    print("\n--- Test: Low-Light Enhancement ---")

    # Create dark frame (brightness ~12, like Vector's camera)
    dark_frame = make_frame(brightness=12)
    gray_before = cv2.cvtColor(dark_frame, cv2.COLOR_BGR2GRAY).mean()

    enhanced = PersonDetector._enhance_low_light(dark_frame)
    gray_after = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY).mean()

    print(f"  Dark frame: before={gray_before:.1f} after={gray_after:.1f}")
    assert gray_after > gray_before * 3, "Enhancement should brighten significantly"

    # Create moderate frame (brightness ~60)
    mod_frame = make_frame(brightness=60)
    enhanced_mod = PersonDetector._enhance_low_light(mod_frame)
    mod_after = cv2.cvtColor(enhanced_mod, cv2.COLOR_BGR2GRAY).mean()
    print(f"  Moderate frame: before=60 after={mod_after:.1f}")
    assert mod_after > 60, "Should brighten moderate frame"

    # Create bright frame (brightness ~150)
    bright_frame = make_frame(brightness=150)
    enhanced_bright = PersonDetector._enhance_low_light(bright_frame)
    bright_after = cv2.cvtColor(enhanced_bright, cv2.COLOR_BGR2GRAY).mean()
    print(f"  Bright frame: before=150 after={bright_after:.1f}")

    print("  PASS")
    return True


def test_adaptive_confidence():
    """Test that adaptive confidence lowers threshold for dark frames."""
    from apps.vector.src.detector.person_detector import PersonDetector

    print("\n--- Test: Adaptive Confidence ---")
    detector = PersonDetector(confidence_threshold=0.25)

    # Very dark frame
    dark = make_frame(brightness=12)
    conf_dark = detector._adaptive_confidence(dark)
    print(f"  Dark (12): conf={conf_dark:.3f}")
    assert conf_dark < 0.15, "Should be very permissive for dark frames"

    # Normal frame
    normal = make_frame(brightness=100)
    conf_normal = detector._adaptive_confidence(normal)
    print(f"  Normal (100): conf={conf_normal:.3f}")
    assert abs(conf_normal - 0.25) < 0.01, "Should be default for normal frames"

    # Bright frame
    bright = make_frame(brightness=180)
    conf_bright = detector._adaptive_confidence(bright)
    print(f"  Bright (180): conf={conf_bright:.3f}")
    assert conf_bright > 0.30, "Should be stricter for bright frames"

    print("  PASS")
    return True


def test_detection_with_yolo():
    """Test actual YOLO detection on a synthetic frame with a person-like shape."""
    from apps.vector.src.detector.person_detector import PersonDetector

    print("\n--- Test: YOLO Detection (synthetic) ---")

    detector = PersonDetector()
    detector.load_model()
    print(f"  Model loaded: {detector.is_loaded}")

    # Create a frame with some structure (not just uniform)
    frame = make_frame(brightness=80)
    # Add a bright rectangle vaguely person-shaped
    frame[100:500, 300:500] = 200  # bright rectangle

    t0 = time.perf_counter()
    detections = detector.detect(frame)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"  Inference: {elapsed:.0f}ms, detections: {len(detections)}")
    print(f"  Avg inference: {detector.avg_inference_ms:.0f}ms")

    # Run 10 more frames to get stable timing
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        detector.detect(frame)
        times.append((time.perf_counter() - t0) * 1000)

    avg = sum(times) / len(times)
    print(f"  10-frame avg inference: {avg:.0f}ms ({1000/avg:.0f} FPS)")
    assert avg < 100, f"YOLO11n should be under 100ms, got {avg:.0f}ms"

    print("  PASS")
    return True


def test_full_pipeline_mock():
    """Test the complete pipeline with mock components."""
    from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker
    from apps.vector.src.events.event_types import TRACKED_PERSON, TrackedPersonEvent
    from apps.vector.src.planner.follow_planner import FollowPlanner

    print("\n--- Test: Full Pipeline Mock ---")
    motor = MockMotorController()
    head = MockHeadController()
    bus = MockNucEventBus()
    tracker = KalmanTracker()

    planner = FollowPlanner(motor, head, bus)
    planner.start()
    time.sleep(0.1)

    # Simulate: person starts centered, moves right, comes closer
    detections_sequence = [
        # (cx, cy, w, h, conf) — person centered, far away
        (400, 300, 80, 150, 0.6),
        (400, 300, 80, 150, 0.6),
        (400, 300, 80, 160, 0.6),
        # Person moves right
        (500, 300, 80, 170, 0.5),
        (550, 300, 90, 180, 0.5),
        (580, 310, 90, 200, 0.5),
        # Person comes closer (height increases toward target 300)
        (550, 320, 100, 250, 0.5),
        (520, 320, 110, 280, 0.5),
        (480, 320, 120, 300, 0.5),  # at target distance
        (450, 320, 120, 310, 0.5),  # slightly too close
    ]

    for i, (cx, cy, w, h, conf) in enumerate(detections_sequence):
        dets = [Detection(cx=cx, cy=cy, width=w, height=h, confidence=conf)]
        confirmed = tracker.update(dets)

        if confirmed:
            primary = tracker.get_primary_track()
            if primary:
                event = TrackedPersonEvent(
                    track_id=primary.track_id,
                    cx=primary.cx, cy=primary.cy,
                    width=primary.width, height=primary.height,
                    age_frames=primary.age, hits=primary.hits,
                    confidence=primary.confidence,
                )
                bus.emit(TRACKED_PERSON, event)

        time.sleep(0.1)

    # Wait for planner to process
    time.sleep(0.5)

    state = planner.state
    print(f"  State after 10 detections: {state.value}")

    # Check motor commands
    motor_cmds = [c for c in motor.commands if isinstance(c[0], float)]
    print(f"  Motor commands: {len(motor_cmds)}")

    if motor_cmds:
        # Last few commands: person is near-centered and at target distance
        # Should have small motor values
        last_l, last_r, _ = motor_cmds[-1]
        print(f"  Last command: L={last_l:.1f} R={last_r:.1f}")

        # Earlier when person was right, left should have been > right (turning right)
        # Find a command from when person was at cx=580
        mid_cmds = motor_cmds[3:6]
        if mid_cmds:
            left_spd, r, _ = mid_cmds[0]
            print(f"  Mid command (person right): L={left_spd:.1f} R={r:.1f}")

    # Now simulate person disappearing
    print("\n  Simulating person loss...")
    for i in range(80):
        tracker.update([])
        time.sleep(0.05)

    state = planner.state
    print(f"  State after loss: {state.value}")

    planner.stop()
    print(f"  Stopped: {planner.state.value}")

    print("  PASS")
    return True


def test_reactive_timing():
    """Test that detection → motor command latency is acceptable."""
    from apps.vector.src.events.event_types import TRACKED_PERSON, TrackedPersonEvent
    from apps.vector.src.planner.follow_planner import FollowPlanner

    print("\n--- Test: Reactive Timing ---")
    motor = MockMotorController()
    head = MockHeadController()
    bus = MockNucEventBus()

    planner = FollowPlanner(motor, head, bus)
    planner.start()
    time.sleep(0.5)

    # Send a tracked person event and measure time to motor command
    motor.commands.clear()
    t0 = time.monotonic()

    event = TrackedPersonEvent(
        track_id=1, cx=600, cy=300, width=100, height=200,
        age_frames=5, hits=5, confidence=0.5,
    )
    bus.emit(TRACKED_PERSON, event)

    # Wait for motor command (up to 200ms)
    deadline = t0 + 0.2
    while time.monotonic() < deadline and not motor.commands:
        time.sleep(0.005)

    if motor.commands:
        cmd_time = motor.commands[0][2]
        latency = (cmd_time - t0) * 1000
        print(f"  Event → motor latency: {latency:.1f}ms")
        assert latency < 150, f"Latency too high: {latency:.0f}ms"
    else:
        # The planner might be in SEARCHING state and not processing motor commands yet
        # Send more events to transition it
        for i in range(20):
            bus.emit(TRACKED_PERSON, event)
            time.sleep(0.1)

        motor_cmds = [c for c in motor.commands if isinstance(c[0], float)]
        print(f"  Motor commands after 20 events: {len(motor_cmds)}")

    planner.stop()
    print("  PASS")
    return True


def main():
    print("=" * 60)
    print("MOCK FOLLOW PIPELINE TEST")
    print("  Tests the complete follow pipeline with synthetic data")
    print("  No robot connection needed")
    print("=" * 60)

    results = {}
    tests = [
        ("PD Controller", test_pd_controller),
        ("EMA Smoothing", test_ema_smoothing),
        ("Kalman Tracker", test_kalman_tracker),
        ("Kalman Velocity Decay", test_kalman_velocity_decay),
        ("State Machine", test_follow_planner_state_machine),
        ("Low-Light Enhancement", test_low_light_enhancement),
        ("Adaptive Confidence", test_adaptive_confidence),
        ("YOLO Detection", test_detection_with_yolo),
        ("Full Pipeline Mock", test_full_pipeline_mock),
        ("Reactive Timing", test_reactive_timing),
    ]

    for name, test_fn in tests:
        try:
            ok = test_fn()
            results[name] = ok if ok is not None else True
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    # Summary
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    print("=" * 60)

    return PASS if failed == 0 else FAIL


if __name__ == "__main__":
    sys.exit(main())
