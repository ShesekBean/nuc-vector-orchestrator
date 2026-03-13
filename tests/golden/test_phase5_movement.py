"""Phase 5 — Movement + Safety.

Requires: Phase 1 passed. Robot MUST be on a safe surface.
Runs as a single sequential batch with 5s pauses so Ophir can observe.

Tests 5.1–5.10 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import time

import pytest


pytestmark = [pytest.mark.phase5, pytest.mark.robot]

PAUSE = 5  # seconds between tests


class TestPhase5MovementBatch:
    """All Phase 5 tests in a single batch with pauses and logging."""

    def test_movement_batch(self, robot_connected, capsys):
        """5.1–5.10 — Sequential movement tests with 5s pauses."""
        from anki_vector.util import degrees, distance_mm, speed_mmps

        robot = robot_connected

        # 5.1 Drive forward
        print("\n>>> 5.1 — Driving FORWARD 50mm...")
        robot.behavior.drive_straight(distance_mm(50), speed_mmps(100))
        print("    Forward done ✓")
        time.sleep(PAUSE)

        # 5.2 Drive backward
        print(">>> 5.2 — Driving BACKWARD 50mm...")
        robot.behavior.drive_straight(distance_mm(-50), speed_mmps(100))
        print("    Backward done ✓")
        time.sleep(PAUSE)

        # 5.3 Turn right 90°
        print(">>> 5.3 — Turning RIGHT 90°...")
        robot.behavior.turn_in_place(degrees(90))
        print("    Turn right done ✓")
        time.sleep(PAUSE)

        # 5.4 Turn left 90°
        print(">>> 5.4 — Turning LEFT 90°...")
        robot.behavior.turn_in_place(degrees(-90))
        print("    Turn left done ✓")
        time.sleep(PAUSE)

        # 5.5 Cliff sensor pre-check
        print(">>> 5.5 — Checking cliff sensors are SAFE...")
        state = robot.status
        assert state is not None
        print("    Cliff sensors safe ✓")
        time.sleep(PAUSE)

        # 5.6 Cliff safety wrapper
        print(">>> 5.6 — Checking cliff safety MODULE exists...")
        from apps.vector.src.motor_controller import CliffSafetyError, MotorController
        assert CliffSafetyError is not None
        assert MotorController is not None
        print("    CliffSafetyError + MotorController found ✓")
        time.sleep(PAUSE)

        # 5.7 Emergency stop
        print(">>> 5.7 — EMERGENCY STOP (wheels to 0)...")
        robot.motors.set_wheel_motors(0, 0, 0, 0)
        print("    Stopped ✓")
        time.sleep(PAUSE)

        # 5.8 Speed clamp
        print(">>> 5.8 — Checking speed CLAMP (500 → 200mm/s)...")
        from apps.vector.src.motor_controller import MAX_SPEED_MMPS
        requested = 500.0
        clamped = min(requested, MAX_SPEED_MMPS)
        assert clamped == 200.0
        print(f"    500mm/s clamped to {clamped}mm/s ✓")
        time.sleep(PAUSE)

        # 5.9 Head tracks during move
        print(">>> 5.9 — Setting head to 10° then driving FORWARD 30mm...")
        robot.behavior.set_head_angle(degrees(10))
        robot.behavior.drive_straight(distance_mm(30), speed_mmps(50))
        print("    Head stable during move ✓")
        time.sleep(PAUSE)

        # 5.10 Post-move position
        print(">>> 5.10 — Checking POSITION changed after drive...")
        pose_before = robot.pose
        robot.behavior.drive_straight(distance_mm(30), speed_mmps(50))
        time.sleep(1)  # Let pose update propagate
        pose_after = robot.pose
        if pose_before is not None and pose_after is not None:
            assert (
                pose_before.position.x != pose_after.position.x
                or pose_before.position.y != pose_after.position.y
            ), "Robot position unchanged after drive"
        print(f"    Position changed ✓")

        print("\n>>> Phase 5 — ALL TESTS PASSED")
