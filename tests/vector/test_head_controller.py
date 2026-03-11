"""Unit tests for Vector head servo controller.

All tests mock anki_vector so they run in CI without a physical robot.
"""

from unittest.mock import MagicMock

import pytest

from apps.vector.src.head_controller import (
    DEFAULT_SPEED_DPS,
    MAX_ANGLE,
    MIN_ANGLE,
    NEUTRAL_ANGLE,
    HeadController,
)


@pytest.fixture
def mock_robot():
    """Create a mock robot with behavior.set_head_angle."""
    robot = MagicMock()
    return robot


@pytest.fixture
def controller(mock_robot):
    """HeadController with default settings."""
    return HeadController(mock_robot)


class TestClamp:
    def test_within_range(self):
        assert HeadController.clamp(10.0) == 10.0

    def test_below_min(self):
        assert HeadController.clamp(-50.0) == MIN_ANGLE

    def test_above_max(self):
        assert HeadController.clamp(90.0) == MAX_ANGLE

    def test_at_min(self):
        assert HeadController.clamp(MIN_ANGLE) == MIN_ANGLE

    def test_at_max(self):
        assert HeadController.clamp(MAX_ANGLE) == MAX_ANGLE

    def test_zero(self):
        assert HeadController.clamp(0.0) == 0.0


class TestSetAngle:
    def test_basic_move(self, controller, mock_robot):
        result = controller.set_angle(20.0)
        assert result == 20.0
        mock_robot.behavior.set_head_angle.assert_called_once()
        # Verify degrees() was called with 20.0
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[0][0] == 20.0  # degrees(20.0) returns 20.0 via conftest stub
        assert args[1]["max_speed"] == DEFAULT_SPEED_DPS

    def test_clamps_high(self, controller, mock_robot):
        result = controller.set_angle(100.0)
        assert result == MAX_ANGLE
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[0][0] == MAX_ANGLE

    def test_clamps_low(self, controller, mock_robot):
        result = controller.set_angle(-90.0)
        assert result == MIN_ANGLE

    def test_custom_speed(self, controller, mock_robot):
        controller.set_angle(0.0, speed_dps=60.0)
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[1]["max_speed"] == 60.0

    def test_speed_floor(self, controller, mock_robot):
        """Speed below 1.0 is clamped to 1.0."""
        controller.set_angle(0.0, speed_dps=-5.0)
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[1]["max_speed"] == 1.0

    def test_updates_last_angle(self, controller):
        assert controller.last_angle is None
        controller.set_angle(15.0)
        assert controller.last_angle == 15.0
        controller.set_angle(-10.0)
        assert controller.last_angle == -10.0


class TestNeutral:
    def test_moves_to_neutral(self, controller, mock_robot):
        result = controller.neutral()
        assert result == NEUTRAL_ANGLE
        mock_robot.behavior.set_head_angle.assert_called_once()
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[0][0] == NEUTRAL_ANGLE

    def test_custom_speed(self, controller, mock_robot):
        controller.neutral(speed_dps=30.0)
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[1]["max_speed"] == 30.0


class TestLookUp:
    def test_moves_to_max(self, controller, mock_robot):
        result = controller.look_up()
        assert result == MAX_ANGLE
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[0][0] == MAX_ANGLE

    def test_custom_speed(self, controller, mock_robot):
        controller.look_up(speed_dps=45.0)
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[1]["max_speed"] == 45.0


class TestLookDown:
    def test_moves_to_min(self, controller, mock_robot):
        result = controller.look_down()
        assert result == MIN_ANGLE
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[0][0] == MIN_ANGLE


class TestCustomDefaults:
    def test_custom_default_speed(self, mock_robot):
        ctrl = HeadController(mock_robot, default_speed_dps=50.0)
        ctrl.set_angle(0.0)
        args = mock_robot.behavior.set_head_angle.call_args
        assert args[1]["max_speed"] == 50.0

    def test_default_speed_floor(self, mock_robot):
        """Default speed below 1.0 is clamped to 1.0."""
        ctrl = HeadController(mock_robot, default_speed_dps=-10.0)
        assert ctrl._default_speed_dps == 1.0
