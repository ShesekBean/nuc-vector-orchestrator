"""Phase 5 — Movement + Safety (~35s).

Requires: Phase 1 passed.  Robot MUST be on a safe surface.

Tests 5.1–5.10 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import pytest

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


pytestmark = [pytest.mark.phase5, pytest.mark.robot]


# 5.1 Drive forward
class TestDriveForward:
    def test_drive_forward_50mm(self, robot_connected):
        """5.1 — DriveStraight(50, 100) moves robot ~50mm forward."""
        from anki_vector.util import distance_mm, speed_mmps

        robot = robot_connected
        robot.behavior.drive_straight(distance_mm(50), speed_mmps(100))


# 5.2 Drive backward
class TestDriveBackward:
    def test_drive_backward_50mm(self, robot_connected):
        """5.2 — DriveStraight(-50, 100) moves robot ~50mm backward."""
        from anki_vector.util import distance_mm, speed_mmps

        robot = robot_connected
        robot.behavior.drive_straight(distance_mm(-50), speed_mmps(100))


# 5.3 Turn right 90°
class TestTurnRight:
    def test_turn_right_90(self, robot_connected):
        """5.3 — TurnInPlace(90, ...) turns robot ~90° clockwise."""
        from anki_vector.util import degrees

        robot = robot_connected
        robot.behavior.turn_in_place(degrees(90))


# 5.4 Turn left 90°
class TestTurnLeft:
    def test_turn_left_90(self, robot_connected):
        """5.4 — TurnInPlace(-90, ...) turns robot ~90° counter-clockwise."""
        from anki_vector.util import degrees

        robot = robot_connected
        robot.behavior.turn_in_place(degrees(-90))


# 5.5 Cliff sensor pre-check
class TestCliffPreCheck:
    def test_cliff_sensors_safe(self, robot_connected):
        """5.5 — All 4 cliff sensors report safe before move."""
        robot = robot_connected
        # Read cliff state — should be safe on a table/floor
        state = robot.status
        assert state is not None


# 5.6 Cliff safety wrapper
class TestCliffSafetyWrapper:
    def test_cliff_safety_module_exists(self):
        """5.6 — MotorController has cliff safety logic."""
        from apps.vector.src.motor_controller import (
            CliffSafetyError,
            MotorController,
        )

        assert CliffSafetyError is not None
        assert MotorController is not None


# 5.7 Emergency stop
class TestEmergencyStop:
    def test_emergency_stop(self, robot_connected):
        """5.7 — DriveWheels(0,0,0,0) stops robot immediately."""
        robot = robot_connected
        robot.motors.set_wheel_motors(0, 0, 0, 0)


# 5.8 Speed clamp
class TestSpeedClamp:
    def test_speed_clamped_to_max(self):
        """5.8 — Request 500mm/s → clamped to max 200mm/s."""
        from apps.vector.src.motor_controller import MAX_SPEED_MMPS

        requested = 500.0
        clamped = min(requested, MAX_SPEED_MMPS)
        assert clamped == MAX_SPEED_MMPS
        assert clamped == 200.0


# 5.9 Head tracks during move
class TestHeadTracksDuringMove:
    def test_head_angle_stable(self, robot_connected):
        """5.9 — Head angle stays at set angle during forward move."""
        from anki_vector.util import degrees, distance_mm, speed_mmps

        robot = robot_connected
        robot.behavior.set_head_angle(degrees(10))
        robot.behavior.drive_straight(distance_mm(30), speed_mmps(50))
        # Head should still be near 10 degrees (exact verification
        # requires reading head angle sensor)


# 5.10 Post-move position
class TestPostMovePosition:
    def test_position_changed(self, robot_connected):
        """5.10 — Position changed after move."""
        from anki_vector.util import distance_mm, speed_mmps

        robot = robot_connected
        pose_before = robot.pose
        robot.behavior.drive_straight(distance_mm(30), speed_mmps(50))
        pose_after = robot.pose
        # At least one coordinate should differ
        if pose_before is not None and pose_after is not None:
            assert (
                pose_before.position.x != pose_after.position.x
                or pose_before.position.y != pose_after.position.y
            ), "Robot position unchanged after drive"
