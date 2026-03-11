"""Unit tests for Vector health check module.

All tests mock the anki_vector.Robot so they can run in CI
without a physical robot connection.
"""

import types
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.bridge.health_check import (
    SubsystemResult,
    _check_subsystem,
    check_battery,
    check_camera,
    check_head,
    check_leds,
    check_lift,
    check_motors,
    check_sensors,
    format_results,
    run_health_check,
)


# --- Fixtures ---


@pytest.fixture
def mock_robot():
    """Create a mock robot with all subsystem attributes."""
    robot = MagicMock()
    robot.name = "Vector-D2C9"

    # Camera: capture_single_image returns an object with raw_image.size
    mock_image = MagicMock()
    mock_image.raw_image.size = (640, 360)
    robot.camera.capture_single_image.return_value = mock_image

    # Battery
    mock_batt = MagicMock()
    mock_batt.battery_volts = 3.85
    mock_batt.battery_level = 2
    robot.get_battery_state.return_value = mock_batt

    # Sensors
    robot.accel = types.SimpleNamespace(x=0.0, y=0.0, z=-9800.0)
    robot.gyro = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
    mock_touch = MagicMock()
    mock_touch.last_sensor_reading = False
    robot.touch = mock_touch

    return robot


# --- SubsystemResult ---


class TestSubsystemResult:
    def test_pass_result(self):
        r = SubsystemResult("test", True, 12.5, "OK")
        assert r.name == "test"
        assert r.passed is True
        assert r.latency_ms == 12.5
        assert r.detail == "OK"

    def test_fail_result(self):
        r = SubsystemResult("test", False, 100.0, "connection refused")
        assert r.passed is False


# --- _check_subsystem ---


class TestCheckSubsystem:
    def test_passing_check(self):
        result = _check_subsystem("ok", lambda: "all good")
        assert result.passed is True
        assert result.detail == "all good"
        assert result.latency_ms >= 0

    def test_failing_check(self):
        def bad():
            raise RuntimeError("broke")
        result = _check_subsystem("bad", bad)
        assert result.passed is False
        assert "broke" in result.detail

    def test_none_return_gives_ok(self):
        result = _check_subsystem("none", lambda: None)
        assert result.passed is True
        assert result.detail == "OK"


# --- Individual subsystem checks ---


class TestMotors:
    def test_pass(self, mock_robot):
        r = check_motors(mock_robot)
        assert r.passed is True
        assert r.name == "motors"
        mock_robot.motors.set_wheel_motors.assert_called()

    def test_fail(self, mock_robot):
        mock_robot.motors.set_wheel_motors.side_effect = RuntimeError("timeout")
        r = check_motors(mock_robot)
        assert r.passed is False


class TestLeds:
    def test_pass(self, mock_robot):
        r = check_leds(mock_robot)
        assert r.passed is True
        assert r.name == "leds"
        # Should have been called 4 times (3 colors + reset)
        assert mock_robot.behavior.set_eye_color.call_count == 4

    def test_fail(self, mock_robot):
        mock_robot.behavior.set_eye_color.side_effect = RuntimeError("LED error")
        r = check_leds(mock_robot)
        assert r.passed is False


class TestCamera:
    def test_pass(self, mock_robot):
        r = check_camera(mock_robot)
        assert r.passed is True
        assert "640x360" in r.detail

    def test_none_image(self, mock_robot):
        mock_robot.camera.capture_single_image.return_value = None
        r = check_camera(mock_robot)
        assert r.passed is False
        assert "None" in r.detail

    def test_small_image(self, mock_robot):
        mock_robot.camera.capture_single_image.return_value.raw_image.size = (160, 90)
        r = check_camera(mock_robot)
        assert r.passed is False
        assert "too small" in r.detail


class TestHead:
    def test_pass(self, mock_robot):
        r = check_head(mock_robot)
        assert r.passed is True
        mock_robot.behavior.set_head_angle.assert_called_once()


class TestLift:
    def test_pass(self, mock_robot):
        r = check_lift(mock_robot)
        assert r.passed is True
        mock_robot.behavior.set_lift_height.assert_called_once_with(0.0)


class TestBattery:
    def test_pass(self, mock_robot):
        r = check_battery(mock_robot)
        assert r.passed is True
        assert "3.85V" in r.detail

    def test_fail(self, mock_robot):
        mock_robot.get_battery_state.side_effect = RuntimeError("no battery")
        r = check_battery(mock_robot)
        assert r.passed is False


class TestSensors:
    def test_pass(self, mock_robot):
        r = check_sensors(mock_robot)
        assert r.passed is True
        assert "accel=" in r.detail

    def test_no_accel(self, mock_robot):
        mock_robot.accel = None
        r = check_sensors(mock_robot)
        assert r.passed is False
        assert "unavailable" in r.detail


# --- run_health_check ---


class TestRunHealthCheck:
    @patch("anki_vector.Robot", create=True)
    def test_all_pass(self, mock_robot_cls, mock_robot):
        mock_robot_cls.return_value = mock_robot
        results = run_health_check()
        assert len(results) == 7
        assert all(r.passed for r in results)
        mock_robot.connect.assert_called_once()
        mock_robot.disconnect.assert_called_once()

    @patch("anki_vector.Robot", create=True)
    def test_connection_failure(self, mock_robot_cls):
        mock_robot = MagicMock()
        mock_robot.connect.side_effect = ConnectionError("WiFi down")
        mock_robot_cls.return_value = mock_robot
        results = run_health_check()
        assert any(not r.passed for r in results)
        assert results[0].name == "connection"


# --- format_results ---


class TestFormatResults:
    def test_healthy_output(self):
        results = [
            SubsystemResult("motors", True, 10.0, "OK"),
            SubsystemResult("camera", True, 25.0, "640x360"),
        ]
        output = format_results(results)
        assert "HEALTHY" in output
        assert "2/2 passed" in output
        assert "motors" in output

    def test_unhealthy_output(self):
        results = [
            SubsystemResult("motors", True, 10.0, "OK"),
            SubsystemResult("camera", False, 0.0, "timeout"),
        ]
        output = format_results(results)
        assert "UNHEALTHY" in output
        assert "1/2 passed" in output
        assert "camera" in output
