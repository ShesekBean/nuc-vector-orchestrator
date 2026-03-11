"""Tests for BatteryMonitor — voltage thresholds, state transitions, and events."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.vector.src.battery_monitor import (
    DEFAULT_CRITICAL_VOLTAGE,
    DEFAULT_LOW_VOLTAGE,
    SEVERITY_CRITICAL,
    SEVERITY_LOW,
    SEVERITY_NORMAL,
    BatteryMonitor,
)
from apps.vector.src.events.event_types import (
    BATTERY_LOW,
    BATTERY_STATE,
    EMERGENCY_STOP,
    BatteryStateEvent,
    BatteryLowEvent,
    EmergencyStopEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus


@pytest.fixture
def bus():
    return NucEventBus()


@pytest.fixture
def robot():
    r = MagicMock()
    r.motors.set_wheel_motors = MagicMock()
    return r


@pytest.fixture
def monitor(robot, bus):
    return BatteryMonitor(robot, bus)


def _emit_battery(bus, voltage, level=2, charging=False, on_charger=False):
    """Helper to emit a battery_state event on the bus."""
    bus.emit(
        BATTERY_STATE,
        BatteryStateEvent(
            voltage=voltage,
            level=level,
            is_charging=charging,
            is_on_charger=on_charger,
        ),
    )


# -- Lifecycle ---------------------------------------------------------------

class TestLifecycle:
    def test_start_subscribes(self, monitor, bus):
        monitor.start()
        assert bus.listener_count(BATTERY_STATE) == 1

    def test_stop_unsubscribes(self, monitor, bus):
        monitor.start()
        monitor.stop()
        assert bus.listener_count(BATTERY_STATE) == 0

    def test_double_start_idempotent(self, monitor, bus):
        monitor.start()
        monitor.start()
        assert bus.listener_count(BATTERY_STATE) == 1

    def test_double_stop_idempotent(self, monitor, bus):
        monitor.start()
        monitor.stop()
        monitor.stop()
        assert bus.listener_count(BATTERY_STATE) == 0

    def test_stop_without_start(self, monitor, bus):
        monitor.stop()  # should not raise
        assert bus.listener_count(BATTERY_STATE) == 0


# -- Normal battery ----------------------------------------------------------

class TestNormalBattery:
    def test_normal_voltage_no_events(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.85)
        assert len(low_events) == 0
        assert monitor.severity == SEVERITY_NORMAL

    def test_state_accessors_updated(self, monitor, bus):
        monitor.start()
        _emit_battery(bus, 3.85, level=2, charging=False, on_charger=False)
        assert monitor.voltage == 3.85
        assert monitor.level == 2
        assert monitor.is_charging is False
        assert monitor.is_on_charger is False
        assert monitor.update_count == 1

    def test_multiple_normal_updates(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        for v in [3.9, 3.85, 3.8, 3.75, 3.7]:
            _emit_battery(bus, v)
        assert len(low_events) == 0
        assert monitor.update_count == 5


# -- Low battery transition --------------------------------------------------

class TestLowBattery:
    def test_low_voltage_emits_battery_low(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.55)
        assert len(low_events) == 1
        assert isinstance(low_events[0], BatteryLowEvent)
        assert low_events[0].severity == SEVERITY_LOW
        assert low_events[0].voltage == 3.55
        assert monitor.severity == SEVERITY_LOW

    def test_low_no_emergency_stop(self, monitor, bus):
        stops = []
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.55)
        assert len(stops) == 0

    def test_repeated_low_no_duplicate_events(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.55)
        _emit_battery(bus, 3.54)
        _emit_battery(bus, 3.53)
        assert len(low_events) == 1  # only transition fires

    def test_exact_low_threshold(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, DEFAULT_LOW_VOLTAGE)
        assert len(low_events) == 1
        assert monitor.severity == SEVERITY_LOW


# -- Critical battery transition ---------------------------------------------

class TestCriticalBattery:
    def test_critical_voltage_emits_both(self, monitor, bus):
        low_events = []
        stops = []
        bus.on(BATTERY_LOW, low_events.append)
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.45)
        assert len(low_events) == 1
        assert low_events[0].severity == SEVERITY_CRITICAL
        assert len(stops) == 1
        assert isinstance(stops[0], EmergencyStopEvent)
        assert stops[0].source == "battery_critical"
        assert monitor.severity == SEVERITY_CRITICAL

    def test_exact_critical_threshold(self, monitor, bus):
        stops = []
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, DEFAULT_CRITICAL_VOLTAGE)
        assert len(stops) == 1
        assert monitor.severity == SEVERITY_CRITICAL

    def test_direct_normal_to_critical(self, monitor, bus):
        """Skip low — go straight to critical."""
        low_events = []
        stops = []
        bus.on(BATTERY_LOW, low_events.append)
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.3)
        assert len(low_events) == 1
        assert low_events[0].severity == SEVERITY_CRITICAL
        assert len(stops) == 1

    def test_low_then_critical(self, monitor, bus):
        """Normal → low → critical fires two separate events."""
        low_events = []
        stops = []
        bus.on(BATTERY_LOW, low_events.append)
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.55)  # low
        _emit_battery(bus, 3.45)  # critical
        assert len(low_events) == 2
        assert low_events[0].severity == SEVERITY_LOW
        assert low_events[1].severity == SEVERITY_CRITICAL
        assert len(stops) == 1


# -- Charging suppresses warnings -------------------------------------------

class TestCharging:
    def test_charging_suppresses_low(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.55, charging=True)
        assert len(low_events) == 0
        assert monitor.severity == SEVERITY_NORMAL
        assert monitor.is_charging is True

    def test_charging_suppresses_critical(self, monitor, bus):
        stops = []
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.3, charging=True)
        assert len(stops) == 0

    def test_charging_clears_low_state(self, monitor, bus):
        monitor.start()
        _emit_battery(bus, 3.55)  # low
        assert monitor.severity == SEVERITY_LOW
        _emit_battery(bus, 3.55, charging=True)  # plugged in
        assert monitor.severity == SEVERITY_NORMAL

    def test_on_charger_tracked(self, monitor, bus):
        monitor.start()
        _emit_battery(bus, 3.85, on_charger=True)
        assert monitor.is_on_charger is True


# -- Recovery ----------------------------------------------------------------

class TestRecovery:
    def test_low_to_normal_recovery(self, monitor, bus):
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.55)  # low
        _emit_battery(bus, 3.85)  # recovered
        assert monitor.severity == SEVERITY_NORMAL
        assert len(low_events) == 1  # no extra event for recovery

    def test_critical_to_normal_recovery(self, monitor, bus):
        monitor.start()
        _emit_battery(bus, 3.3)  # critical
        assert monitor.severity == SEVERITY_CRITICAL
        _emit_battery(bus, 3.85)  # recovered
        assert monitor.severity == SEVERITY_NORMAL


# -- Custom thresholds -------------------------------------------------------

class TestCustomThresholds:
    def test_custom_low_threshold(self, robot, bus):
        monitor = BatteryMonitor(robot, bus, low_voltage=3.7)
        low_events = []
        bus.on(BATTERY_LOW, low_events.append)
        monitor.start()
        _emit_battery(bus, 3.65)
        assert len(low_events) == 1

    def test_custom_critical_threshold(self, robot, bus):
        monitor = BatteryMonitor(robot, bus, critical_voltage=3.4)
        stops = []
        bus.on(EMERGENCY_STOP, stops.append)
        monitor.start()
        _emit_battery(bus, 3.45)  # above custom critical
        assert len(stops) == 0
        _emit_battery(bus, 3.35)  # below custom critical
        assert len(stops) == 1


# -- SDK event bridge battery emission ---------------------------------------

class TestSdkBridgeBattery:
    def test_bridge_emits_battery_state(self):
        """SdkEventBridge emits BATTERY_STATE from robot_state events."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        robot = MagicMock()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)

        received = []
        bus.on(BATTERY_STATE, received.append)

        # Simulate an SDK robot_state message with battery fields
        msg = MagicMock()
        msg.cliff_detected_flags = 0
        msg.touch_detected = False
        msg.battery_voltage = 3.82
        msg.battery_level = 2
        msg.is_charging = False
        msg.is_on_charger_platform = True

        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, BatteryStateEvent)
        assert evt.voltage == 3.82
        assert evt.level == 2
        assert evt.is_charging is False
        assert evt.is_on_charger is True

    def test_bridge_no_battery_when_voltage_missing(self):
        """No battery_state event when msg lacks battery_voltage."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        robot = MagicMock()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)

        received = []
        bus.on(BATTERY_STATE, received.append)

        msg = MagicMock(spec=[])  # no attributes
        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(received) == 0


# -- Integration: bridge → monitor ------------------------------------------

class TestIntegration:
    def test_bridge_to_monitor_low_battery(self):
        """End-to-end: SDK robot_state → bridge → bus → monitor → battery_low."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        robot = MagicMock()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)
        monitor = BatteryMonitor(robot, bus)
        monitor.start()

        low_events = []
        bus.on(BATTERY_LOW, low_events.append)

        # Simulate low battery SDK event
        msg = MagicMock()
        msg.cliff_detected_flags = 0
        msg.touch_detected = False
        msg.battery_voltage = 3.55
        msg.battery_level = 1
        msg.is_charging = False
        msg.is_on_charger_platform = False

        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(low_events) == 1
        assert low_events[0].severity == SEVERITY_LOW
        assert monitor.voltage == 3.55

    def test_bridge_to_monitor_critical_stops_motors(self):
        """End-to-end: critical battery → emergency_stop emitted."""
        from apps.vector.src.events.sdk_events import SdkEventBridge

        robot = MagicMock()
        bus = NucEventBus()
        bridge = SdkEventBridge(robot, bus)
        monitor = BatteryMonitor(robot, bus)
        monitor.start()

        stops = []
        bus.on(EMERGENCY_STOP, stops.append)

        msg = MagicMock()
        msg.cliff_detected_flags = 0
        msg.touch_detected = False
        msg.battery_voltage = 3.3
        msg.battery_level = 0
        msg.is_charging = False
        msg.is_on_charger_platform = False

        bridge._on_robot_state(robot, "robot_state", msg)

        assert len(stops) == 1
        assert stops[0].source == "battery_critical"
        assert monitor.severity == SEVERITY_CRITICAL
