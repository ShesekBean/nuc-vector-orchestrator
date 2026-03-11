"""Tests for the Vector sensor handler — cliff detection and touch events."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    EMERGENCY_STOP,
    TOUCH_DETECTED,
    CliffTriggeredEvent,
    EmergencyStopEvent,
    TouchDetectedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.sensor_handler import SensorHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_robot():
    """Create a mock robot with motors API."""
    robot = MagicMock()
    robot.motors.set_wheel_motors = MagicMock()
    return robot


# ---------------------------------------------------------------------------
# SensorHandler lifecycle tests
# ---------------------------------------------------------------------------


class TestSensorHandlerLifecycle:
    def test_start_subscribes_to_events(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1
        assert bus.listener_count(CLIFF_TRIGGERED) == 1
        assert bus.listener_count(TOUCH_DETECTED) == 1
        handler.stop()

    def test_stop_unsubscribes_from_events(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()
        handler.stop()
        assert bus.listener_count(EMERGENCY_STOP) == 0
        assert bus.listener_count(CLIFF_TRIGGERED) == 0
        assert bus.listener_count(TOUCH_DETECTED) == 0

    def test_double_start_is_noop(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()
        handler.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1
        handler.stop()

    def test_double_stop_is_noop(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()
        handler.stop()
        handler.stop()  # should not raise

    def test_cliff_count_resets_on_start(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=1))
        assert handler.cliff_count == 1
        handler.stop()
        handler.start()
        assert handler.cliff_count == 0
        handler.stop()


# ---------------------------------------------------------------------------
# Cliff detection tests
# ---------------------------------------------------------------------------


class TestCliffDetection:
    def test_emergency_stop_triggers_motor_stop(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()

        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))

        robot.motors.set_wheel_motors.assert_called_once_with(0.0, 0.0, 0.0, 0.0)
        handler.stop()

    def test_connection_lost_also_stops_motors(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()

        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="connection_lost"))

        robot.motors.set_wheel_motors.assert_called_once_with(0.0, 0.0, 0.0, 0.0)
        handler.stop()

    def test_cliff_triggered_increments_count(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()

        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=1))
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=3))
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=2))

        assert handler.cliff_count == 3
        handler.stop()

    def test_motor_stop_failure_does_not_crash(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        robot.motors.set_wheel_motors.side_effect = RuntimeError("disconnected")
        handler = SensorHandler(robot, bus)
        handler.start()

        # Should not raise
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))
        handler.stop()

    def test_multiple_cliff_events_stop_motors_each_time(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.start()

        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))

        assert robot.motors.set_wheel_motors.call_count == 2
        handler.stop()


# ---------------------------------------------------------------------------
# Touch detection tests
# ---------------------------------------------------------------------------


class TestTouchDetection:
    def test_touch_fires_callback(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        received = []
        handler.on_touch(received.append)

        evt = TouchDetectedEvent(location="head", is_pressed=True)
        bus.emit(TOUCH_DETECTED, evt)

        assert len(received) == 1
        assert received[0].location == "head"
        handler.stop()

    def test_touch_debounce(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.5)
        handler.start()

        received = []
        handler.on_touch(received.append)

        evt = TouchDetectedEvent()
        bus.emit(TOUCH_DETECTED, evt)
        bus.emit(TOUCH_DETECTED, evt)  # within debounce window
        bus.emit(TOUCH_DETECTED, evt)  # within debounce window

        # Only first should fire
        assert len(received) == 1
        handler.stop()

    def test_touch_after_debounce_window(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.01)
        handler.start()

        received = []
        handler.on_touch(received.append)

        evt = TouchDetectedEvent()
        bus.emit(TOUCH_DETECTED, evt)
        time.sleep(0.02)  # exceed debounce window
        bus.emit(TOUCH_DETECTED, evt)

        assert len(received) == 2
        handler.stop()

    def test_off_touch_removes_callback(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        received = []
        handler.on_touch(received.append)
        handler.off_touch(received.append)

        bus.emit(TOUCH_DETECTED, TouchDetectedEvent())

        assert len(received) == 0
        handler.stop()

    def test_off_touch_nonexistent_is_noop(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus)
        handler.off_touch(lambda e: None)  # should not raise

    def test_multiple_touch_callbacks(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        a, b = [], []
        handler.on_touch(a.append)
        handler.on_touch(b.append)

        bus.emit(TOUCH_DETECTED, TouchDetectedEvent())

        assert len(a) == 1
        assert len(b) == 1
        handler.stop()

    def test_touch_callback_exception_does_not_block_others(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        received = []

        def bad_cb(_evt):
            raise RuntimeError("boom")

        handler.on_touch(bad_cb)
        handler.on_touch(received.append)

        bus.emit(TOUCH_DETECTED, TouchDetectedEvent())

        assert len(received) == 1
        handler.stop()

    def test_duplicate_touch_callback_ignored(self):
        bus = NucEventBus()
        robot = _make_mock_robot()
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        received = []
        handler.on_touch(received.append)
        handler.on_touch(received.append)  # duplicate

        bus.emit(TOUCH_DETECTED, TouchDetectedEvent())

        assert len(received) == 1
        handler.stop()


# ---------------------------------------------------------------------------
# Integration: SdkEventBridge + SensorHandler
# ---------------------------------------------------------------------------


class TestSdkBridgeIntegration:
    def test_cliff_from_sdk_stops_motors(self):
        """Simulate the full path: SDK robot_state -> bridge -> bus -> handler -> motor stop."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        bus = NucEventBus()
        robot = _make_mock_robot()

        bridge = SdkEventBridge(robot, bus)
        handler = SensorHandler(robot, bus)
        handler.start()

        # Simulate SDK cliff event
        msg = MagicMock()
        msg.cliff_detected_flags = 5
        msg.touch_detected = False
        bridge._on_robot_state(robot, "robot_state", msg)

        robot.motors.set_wheel_motors.assert_called_once_with(0.0, 0.0, 0.0, 0.0)
        assert handler.cliff_count == 1
        handler.stop()

    def test_touch_from_sdk_fires_callback(self):
        """Simulate: SDK robot_state (touch) -> bridge -> bus -> handler -> callback."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        bus = NucEventBus()
        robot = _make_mock_robot()

        bridge = SdkEventBridge(robot, bus)
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        received = []
        handler.on_touch(received.append)

        # Simulate SDK touch event
        msg = MagicMock()
        msg.cliff_detected_flags = 0
        msg.touch_detected = True
        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(received) == 1
        assert received[0].location == "head"
        handler.stop()

    def test_cliff_and_touch_simultaneous(self):
        """Both cliff and touch in the same robot_state message."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        bus = NucEventBus()
        robot = _make_mock_robot()

        bridge = SdkEventBridge(robot, bus)
        handler = SensorHandler(robot, bus, touch_debounce_s=0.0)
        handler.start()

        touch_received = []
        handler.on_touch(touch_received.append)

        msg = MagicMock()
        msg.cliff_detected_flags = 3
        msg.touch_detected = True
        bridge._on_robot_state(robot, "robot_state", msg)

        # Motor stop from cliff
        robot.motors.set_wheel_motors.assert_called_once_with(0.0, 0.0, 0.0, 0.0)
        assert handler.cliff_count == 1
        # Touch callback also fired
        assert len(touch_received) == 1
        handler.stop()


# ---------------------------------------------------------------------------
# Event payload tests
# ---------------------------------------------------------------------------


class TestNewEventPayloads:
    def test_cliff_triggered_event(self):
        evt = CliffTriggeredEvent(cliff_flags=5, timestamp_ms=123.4)
        assert evt.cliff_flags == 5
        assert evt.timestamp_ms == 123.4

    def test_cliff_triggered_frozen(self):
        evt = CliffTriggeredEvent(cliff_flags=1)
        with pytest.raises(AttributeError):
            evt.cliff_flags = 2  # type: ignore[misc]

    def test_touch_detected_event(self):
        evt = TouchDetectedEvent(location="head", is_pressed=True)
        assert evt.location == "head"
        assert evt.is_pressed is True

    def test_touch_detected_defaults(self):
        evt = TouchDetectedEvent()
        assert evt.location == "head"
        assert evt.is_pressed is True

    def test_touch_detected_frozen(self):
        evt = TouchDetectedEvent()
        with pytest.raises(AttributeError):
            evt.location = "back"  # type: ignore[misc]
