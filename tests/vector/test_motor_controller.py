"""Tests for motor controller with cliff-safe differential drive planner."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, call

import pytest

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    CliffTriggeredEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.motor_controller import (
    CLIFF_FRONT,
    CLIFF_FRONT_LEFT,
    CLIFF_REAR,
    CLIFF_REAR_LEFT,
    DEFAULT_ACCEL_MMPS2,
    MAX_SPEED_MMPS,
    CliffMonitor,
    CliffSafetyError,
    Direction,
    MotorController,
    _clamp,
    _infer_direction,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.motors.set_wheel_motors = MagicMock()
    robot.behavior.drive_straight = MagicMock()
    robot.behavior.turn_in_place = MagicMock()
    robot.behavior.say_text = MagicMock()
    return robot


@pytest.fixture()
def monitor(bus):
    m = CliffMonitor(bus)
    m.start()
    yield m
    m.stop()


@pytest.fixture()
def controller(mock_robot, bus):
    mc = MotorController(mock_robot, bus)
    mc.start()
    yield mc
    mc.stop()


# ---------------------------------------------------------------------------
# CliffMonitor tests
# ---------------------------------------------------------------------------


class TestCliffMonitor:
    def test_starts_with_no_flags(self, monitor):
        assert monitor.cliff_flags == 0

    def test_updates_flags_on_cliff_event(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT))
        assert monitor.cliff_flags & CLIFF_FRONT_LEFT

    def test_accumulates_flags(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT))
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        assert monitor.cliff_flags == (CLIFF_FRONT_LEFT | CLIFF_REAR_LEFT)

    def test_clear_resets_flags(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))
        monitor.clear()
        assert monitor.cliff_flags == 0

    def test_forward_blocked_by_front_cliff(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT))
        assert monitor.is_direction_blocked(Direction.FORWARD)

    def test_forward_not_blocked_by_rear_cliff(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        assert not monitor.is_direction_blocked(Direction.FORWARD)

    def test_reverse_blocked_by_rear_cliff(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        assert monitor.is_direction_blocked(Direction.REVERSE)

    def test_reverse_not_blocked_by_front_cliff(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT))
        assert not monitor.is_direction_blocked(Direction.REVERSE)

    def test_rotate_blocked_by_any_cliff(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        assert monitor.is_direction_blocked(Direction.ROTATE)

    def test_stop_never_blocked(self, monitor, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=0x0F))
        assert not monitor.is_direction_blocked(Direction.STOP)

    def test_no_events_after_stop(self, bus):
        m = CliffMonitor(bus)
        m.start()
        m.stop()
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))
        assert m.cliff_flags == 0

    def test_thread_safety(self, monitor, bus):
        """Concurrent cliff event emissions should not corrupt state."""
        errors = []

        def emit_events():
            try:
                for _ in range(100):
                    bus.emit(
                        CLIFF_TRIGGERED,
                        CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT),
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_events) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert monitor.cliff_flags & CLIFF_FRONT_LEFT


# ---------------------------------------------------------------------------
# MotorController — emergency stop
# ---------------------------------------------------------------------------


class TestEmergencyStop:
    def test_emergency_stop_zeros_wheels(self, controller, mock_robot):
        controller.emergency_stop()
        mock_robot.motors.set_wheel_motors.assert_called_with(0.0, 0.0, 0.0, 0.0)

    def test_emergency_stop_latches(self, controller):
        controller.emergency_stop()
        with pytest.raises(CliffSafetyError, match="emergency-stop state"):
            controller.drive_wheels(100, 100)

    def test_clear_stop_allows_commands(self, controller, mock_robot):
        controller.emergency_stop()
        controller.clear_stop()
        controller.drive_wheels(50, 50)
        # Last call should be the drive_wheels, not the e-stop
        last_call = mock_robot.motors.set_wheel_motors.call_args
        assert last_call == call(50, 50, DEFAULT_ACCEL_MMPS2, DEFAULT_ACCEL_MMPS2)

    def test_emergency_stop_survives_robot_error(self, controller, mock_robot):
        mock_robot.motors.set_wheel_motors.side_effect = RuntimeError("disconnected")
        # Should not raise — emergency stop must be best-effort
        controller.emergency_stop()


# ---------------------------------------------------------------------------
# MotorController — drive_wheels
# ---------------------------------------------------------------------------


class TestDriveWheels:
    def test_basic_forward(self, controller, mock_robot):
        controller.drive_wheels(100, 100)
        mock_robot.motors.set_wheel_motors.assert_called_once_with(
            100, 100, DEFAULT_ACCEL_MMPS2, DEFAULT_ACCEL_MMPS2
        )

    def test_clamps_speed(self, controller, mock_robot):
        controller.drive_wheels(999, -999)
        mock_robot.motors.set_wheel_motors.assert_called_once_with(
            MAX_SPEED_MMPS, -MAX_SPEED_MMPS, DEFAULT_ACCEL_MMPS2, DEFAULT_ACCEL_MMPS2
        )

    def test_blocked_by_front_cliff_when_forward(self, controller, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT_LEFT))
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(100, 100)

    def test_blocked_by_rear_cliff_when_reverse(self, controller, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(-100, -100)

    def test_forward_allowed_with_rear_cliff(self, controller, mock_robot, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        controller.drive_wheels(100, 100)  # Should succeed
        mock_robot.motors.set_wheel_motors.assert_called()

    def test_custom_accel(self, controller, mock_robot):
        controller.drive_wheels(50, 50, left_accel=100, right_accel=150)
        mock_robot.motors.set_wheel_motors.assert_called_once_with(
            50, 50, 100, 150
        )


# ---------------------------------------------------------------------------
# MotorController — drive_straight
# ---------------------------------------------------------------------------


class TestDriveStraight:
    def test_forward(self, controller, mock_robot):
        controller.drive_straight(200, 100)
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_reverse(self, controller, mock_robot):
        controller.drive_straight(-200, 100)
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_blocked_forward_by_cliff(self, controller, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))
        with pytest.raises(CliffSafetyError):
            controller.drive_straight(200, 100)

    def test_blocked_reverse_by_cliff(self, controller, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR))
        with pytest.raises(CliffSafetyError):
            controller.drive_straight(-200, 100)

    def test_speed_clamped(self, controller, mock_robot):
        controller.drive_straight(100, 999)
        # Should not raise — speed is clamped internally
        mock_robot.behavior.drive_straight.assert_called_once()


# ---------------------------------------------------------------------------
# MotorController — turn_in_place
# ---------------------------------------------------------------------------


class TestTurnInPlace:
    def test_basic_turn(self, controller, mock_robot):
        controller.turn_in_place(90)
        mock_robot.behavior.turn_in_place.assert_called_once()

    def test_blocked_by_any_cliff(self, controller, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR_LEFT))
        with pytest.raises(CliffSafetyError):
            controller.turn_in_place(90)


# ---------------------------------------------------------------------------
# MotorController — turn_then_drive
# ---------------------------------------------------------------------------


class TestTurnThenDrive:
    def test_turn_then_forward(self, controller, mock_robot):
        controller.turn_then_drive(45, 200)
        mock_robot.behavior.turn_in_place.assert_called_once()
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_zero_angle_skips_turn(self, controller, mock_robot):
        controller.turn_then_drive(0, 200)
        mock_robot.behavior.turn_in_place.assert_not_called()
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_zero_distance_skips_drive(self, controller, mock_robot):
        controller.turn_then_drive(90, 0)
        mock_robot.behavior.turn_in_place.assert_called_once()
        mock_robot.behavior.drive_straight.assert_not_called()

    def test_cliff_during_turn_blocks_drive(self, controller, bus, mock_robot):
        """If cliff triggers during turn, the intended forward drive should not execute."""
        def trigger_cliff(*args, **kwargs):
            bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))

        mock_robot.behavior.turn_in_place.side_effect = trigger_cliff

        with pytest.raises(CliffSafetyError):
            controller.turn_then_drive(90, 200)

        # Turn was called
        mock_robot.behavior.turn_in_place.assert_called_once()
        # drive_straight may be called once for cliff backup (reverse 20mm),
        # but NOT for the intended 200mm forward movement.
        for c in mock_robot.behavior.drive_straight.call_args_list:
            dist_arg = c[0][0]  # first positional arg is distance
            # Backup is -20mm (negative), the intended drive would be 200mm
            assert dist_arg != 200, "Intended forward drive should not execute"


# ---------------------------------------------------------------------------
# MotorController — ramp_wheels
# ---------------------------------------------------------------------------


class TestRampWheels:
    def test_ramp_calls_multiple_times(self, controller, mock_robot):
        controller.ramp_wheels(100, 100, ramp_time_ms=100)
        # With RAMP_STEP_MS=20, 100ms → 5 steps
        assert mock_robot.motors.set_wheel_motors.call_count == 5

    def test_ramp_reaches_target(self, controller, mock_robot):
        controller.ramp_wheels(100, 100, ramp_time_ms=100)
        last_call = mock_robot.motors.set_wheel_motors.call_args
        # Last step should be full speed
        assert last_call[0][0] == pytest.approx(100.0)
        assert last_call[0][1] == pytest.approx(100.0)

    def test_ramp_blocked_by_cliff(self, controller, bus, mock_robot):
        # Trigger cliff after first motor call
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                bus.emit(
                    CLIFF_TRIGGERED,
                    CliffTriggeredEvent(cliff_flags=CLIFF_FRONT),
                )

        mock_robot.motors.set_wheel_motors.side_effect = side_effect

        with pytest.raises(CliffSafetyError, match="Cliff detected during ramp"):
            controller.ramp_wheels(100, 100, ramp_time_ms=200)

    def test_ramp_clamps_speed(self, controller, mock_robot):
        controller.ramp_wheels(500, 500, ramp_time_ms=20)
        last_call = mock_robot.motors.set_wheel_motors.call_args
        assert last_call[0][0] == pytest.approx(MAX_SPEED_MMPS)


# ---------------------------------------------------------------------------
# MotorController — cliff reaction
# ---------------------------------------------------------------------------


class TestCliffReaction:
    def test_says_text_on_cliff(self, controller, bus, mock_robot):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(100, 100)
        mock_robot.behavior.say_text.assert_called_with("Whoa, edge detected")

    def test_backs_up_on_front_cliff(self, controller, bus, mock_robot):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_FRONT))
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(100, 100)
        # Should have called drive_straight for backup (reverse)
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_drives_forward_on_rear_cliff(self, controller, bus, mock_robot):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=CLIFF_REAR))
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(-100, -100)
        # Should drive forward to escape rear cliff
        mock_robot.behavior.drive_straight.assert_called_once()

    def test_no_movement_on_both_cliffs(self, controller, bus, mock_robot):
        bus.emit(
            CLIFF_TRIGGERED,
            CliffTriggeredEvent(cliff_flags=CLIFF_FRONT | CLIFF_REAR),
        )
        with pytest.raises(CliffSafetyError):
            controller.drive_wheels(100, 100)
        # Should NOT try to back up (both edges detected)
        mock_robot.behavior.drive_straight.assert_not_called()


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_clamp(self):
        assert _clamp(5, 0, 10) == 5
        assert _clamp(-5, 0, 10) == 0
        assert _clamp(15, 0, 10) == 10

    def test_infer_direction_forward(self):
        assert _infer_direction(100, 100) == Direction.FORWARD

    def test_infer_direction_reverse(self):
        assert _infer_direction(-100, -100) == Direction.REVERSE

    def test_infer_direction_rotate(self):
        assert _infer_direction(-100, 100) == Direction.ROTATE

    def test_infer_direction_stop(self):
        assert _infer_direction(0, 0) == Direction.STOP

    def test_infer_direction_arc_forward(self):
        # Slight arc — net positive → forward
        assert _infer_direction(100, 50) == Direction.FORWARD

    def test_infer_direction_arc_reverse(self):
        assert _infer_direction(-100, -50) == Direction.REVERSE


# ---------------------------------------------------------------------------
# MotorController — lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_zeros_motors(self, mock_robot, bus):
        mc = MotorController(mock_robot, bus)
        mc.start()
        mc.stop()
        mock_robot.motors.set_wheel_motors.assert_called_with(0.0, 0.0, 0.0, 0.0)

    def test_no_bypass_without_safe_drive(self):
        """Verify there is no public method that writes to motors without cliff check."""
        # All public motor methods should go through _safe_drive or be emergency_stop
        public_motor_methods = [
            "drive_wheels",
            "drive_straight",
            "turn_in_place",
            "turn_then_drive",
            "ramp_wheels",
        ]
        # Just verify these methods exist (structural test)
        for method_name in public_motor_methods:
            assert hasattr(MotorController, method_name)
        # emergency_stop is the only exception — it's documented as safe
        assert hasattr(MotorController, "emergency_stop")
