"""Unit tests for LiftController."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call

import pytest

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    LIFT_HEIGHT_CHANGED,
    EmergencyStopEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.lift_controller import (
    LiftController,
    _clamp,
)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.behavior.set_lift_height = MagicMock()
    return robot


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def ctrl(mock_robot, bus):
    """Controller with auto-stow disabled for deterministic tests."""
    return LiftController(mock_robot, bus, auto_stow_timeout_s=0)


@pytest.fixture()
def ctrl_auto_stow(mock_robot, bus):
    """Controller with short auto-stow timeout for timer tests."""
    return LiftController(mock_robot, bus, auto_stow_timeout_s=0.2)


# --- _clamp -----------------------------------------------------------------


class TestClamp:
    def test_within_range(self):
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_below_min(self):
        assert _clamp(-0.5, 0.0, 1.0) == 0.0

    def test_above_max(self):
        assert _clamp(1.5, 0.0, 1.0) == 1.0

    def test_at_boundaries(self):
        assert _clamp(0.0, 0.0, 1.0) == 0.0
        assert _clamp(1.0, 0.0, 1.0) == 1.0


# --- Lifecycle --------------------------------------------------------------


class TestLifecycle:
    def test_start_stop(self, ctrl, bus):
        ctrl.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1
        ctrl.stop()
        assert bus.listener_count(EMERGENCY_STOP) == 0

    def test_double_start_is_safe(self, ctrl, bus):
        ctrl.start()
        ctrl.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1

    def test_double_stop_is_safe(self, ctrl):
        ctrl.start()
        ctrl.stop()
        ctrl.stop()  # should not raise

    def test_move_before_start_returns_false(self, ctrl):
        assert ctrl.move_to(0.5) is False


# --- move_to ----------------------------------------------------------------


class TestMoveTo:
    def test_basic_move(self, ctrl, mock_robot):
        ctrl.start()
        assert ctrl.move_to(0.6) is True
        mock_robot.behavior.set_lift_height.assert_called_once_with(0.6)

    def test_clamps_below_zero(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to(-0.5)
        mock_robot.behavior.set_lift_height.assert_called_once_with(0.0)

    def test_clamps_above_one(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to(2.0)
        mock_robot.behavior.set_lift_height.assert_called_once_with(1.0)

    def test_with_speed(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to(0.5, speed=5.0)
        mock_robot.behavior.set_lift_height.assert_called_once_with(
            0.5, max_speed=5.0,
        )

    def test_updates_target(self, ctrl):
        ctrl.start()
        ctrl.move_to(0.7)
        assert ctrl.current_target == pytest.approx(0.7)

    def test_sdk_exception_returns_false(self, ctrl, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_lift_height.side_effect = RuntimeError("oops")
        assert ctrl.move_to(0.5) is False

    def test_emits_event(self, ctrl, bus):
        received = []
        bus.on(LIFT_HEIGHT_CHANGED, lambda e: received.append(e))
        ctrl.start()
        ctrl.move_to(0.4)
        assert len(received) == 1
        assert received[0].height == pytest.approx(0.4)
        assert received[0].preset is None


# --- move_to_preset ---------------------------------------------------------


class TestPresets:
    def test_stowed(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to_preset("stowed")
        mock_robot.behavior.set_lift_height.assert_called_with(0.0)

    def test_carry(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to_preset("carry")
        mock_robot.behavior.set_lift_height.assert_called_with(0.5)

    def test_high(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to_preset("high")
        mock_robot.behavior.set_lift_height.assert_called_with(0.8)

    def test_case_insensitive(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to_preset("CARRY")
        mock_robot.behavior.set_lift_height.assert_called_with(0.5)

    def test_unknown_preset_raises(self, ctrl):
        ctrl.start()
        with pytest.raises(ValueError, match="Unknown preset"):
            ctrl.move_to_preset("nonexistent")

    def test_preset_emits_event_with_name(self, ctrl, bus):
        received = []
        bus.on(LIFT_HEIGHT_CHANGED, lambda e: received.append(e))
        ctrl.start()
        ctrl.move_to_preset("carry")
        # Single event with preset name populated
        assert len(received) == 1
        assert received[0].preset == "carry"
        assert received[0].height == pytest.approx(0.5)


# --- stow convenience ------------------------------------------------------


class TestStow:
    def test_stow(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.move_to(0.8)
        ctrl.stow()
        mock_robot.behavior.set_lift_height.assert_called_with(0.0)
        assert ctrl.is_stowed is True


# --- is_stowed property -----------------------------------------------------


class TestIsStowed:
    def test_initial_is_stowed(self, ctrl):
        assert ctrl.is_stowed is True

    def test_not_stowed_after_move(self, ctrl):
        ctrl.start()
        ctrl.move_to(0.5)
        assert ctrl.is_stowed is False

    def test_stowed_after_stow(self, ctrl):
        ctrl.start()
        ctrl.move_to(0.5)
        ctrl.stow()
        assert ctrl.is_stowed is True


# --- Emergency stop ---------------------------------------------------------


class TestEmergencyStop:
    def test_blocks_move(self, ctrl, bus):
        ctrl.start()
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="test"))
        assert ctrl.move_to(0.5) is False

    def test_clear_allows_move(self, ctrl, bus, mock_robot):
        ctrl.start()
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="test"))
        ctrl.clear_emergency_stop()
        assert ctrl.move_to(0.5) is True


# --- Auto-stow timer -------------------------------------------------------


class TestAutoStow:
    def test_auto_stow_fires(self, ctrl_auto_stow, mock_robot):
        ctrl_auto_stow.start()
        ctrl_auto_stow.move_to(0.8)
        mock_robot.behavior.set_lift_height.reset_mock()
        # Wait for auto-stow (0.2s timeout + margin)
        time.sleep(0.5)
        # Should have stowed
        mock_robot.behavior.set_lift_height.assert_called_with(0.0)
        assert ctrl_auto_stow.is_stowed is True

    def test_auto_stow_reset_on_move(self, ctrl_auto_stow, mock_robot):
        ctrl_auto_stow.start()
        ctrl_auto_stow.move_to(0.8)
        time.sleep(0.1)
        # Move again before timeout — should reset timer
        ctrl_auto_stow.move_to(0.6)
        mock_robot.behavior.set_lift_height.reset_mock()
        time.sleep(0.1)
        # Should NOT have stowed yet (timer was reset)
        stow_calls = [
            c for c in mock_robot.behavior.set_lift_height.call_args_list
            if c == call(0.0)
        ]
        assert len(stow_calls) == 0

    def test_auto_stow_cancelled_on_stop(self, ctrl_auto_stow, mock_robot):
        ctrl_auto_stow.start()
        ctrl_auto_stow.move_to(0.8)
        ctrl_auto_stow.stop()
        mock_robot.behavior.set_lift_height.reset_mock()
        time.sleep(0.5)
        # Timer was cancelled — should NOT have stowed
        mock_robot.behavior.set_lift_height.assert_not_called()

    def test_no_auto_stow_when_disabled(self, ctrl, mock_robot):
        """ctrl fixture has auto_stow_timeout_s=0 (disabled)."""
        ctrl.start()
        ctrl.move_to(0.8)
        mock_robot.behavior.set_lift_height.reset_mock()
        time.sleep(0.3)
        mock_robot.behavior.set_lift_height.assert_not_called()

    def test_no_auto_stow_when_already_stowed(self, ctrl_auto_stow):
        ctrl_auto_stow.start()
        ctrl_auto_stow.stow()
        # Timer should not be set since we're already stowed
        assert ctrl_auto_stow._stow_timer is None


# --- Thread safety ----------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_moves(self, ctrl, mock_robot):
        ctrl.start()
        errors = []

        def mover(height):
            try:
                ctrl.move_to(height)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=mover, args=(h / 10.0,)) for h in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        assert mock_robot.behavior.set_lift_height.call_count == 10
