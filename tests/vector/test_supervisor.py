"""Unit tests for VectorSupervisor."""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.events.event_types import EmergencyStopEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.supervisor import (
    WIFI_STATE,
    ComponentInfo,
    SupervisorState,
    VectorSupervisor,
    WifiStateEvent,
    _sd_notify_ready,
)


# --- Fixtures ---------------------------------------------------------------


def _make_mock_component(name: str = "test", has_start: bool = True) -> MagicMock:
    """Create a mock component with start/stop methods."""
    comp = MagicMock()
    comp.is_alive.return_value = True
    if not has_start:
        del comp.start
        del comp.stop
    return comp


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def supervisor():
    """VectorSupervisor without connecting to a real robot."""
    s = VectorSupervisor(serial="test-serial")
    yield s
    # Ensure cleanup
    if s.state not in (SupervisorState.INIT, SupervisorState.STOPPED):
        s._shutdown_event.set()
        s._shutdown()


# --- ComponentInfo ----------------------------------------------------------


class TestComponentInfo:
    def test_initial_state(self):
        comp = ComponentInfo("test", lambda: MagicMock(), 1)
        assert comp.name == "test"
        assert comp.start_order == 1
        assert not comp.is_critical
        assert comp.requires_connection
        assert not comp.is_started
        assert comp.instance is None

    def test_critical_flag(self):
        comp = ComponentInfo("test", lambda: MagicMock(), 1, is_critical=True)
        assert comp.is_critical

    def test_no_connection_flag(self):
        comp = ComponentInfo(
            "test", lambda: MagicMock(), 1, requires_connection=False
        )
        assert not comp.requires_connection


# --- Supervisor State -------------------------------------------------------


class TestSupervisorState:
    def test_initial_state(self, supervisor):
        assert supervisor.state == SupervisorState.INIT

    def test_set_state(self, supervisor):
        supervisor._set_state(SupervisorState.RUNNING)
        assert supervisor.state == SupervisorState.RUNNING

    def test_state_is_thread_safe(self, supervisor):
        results = []

        def setter():
            for _ in range(100):
                supervisor._set_state(SupervisorState.RUNNING)
                results.append(supervisor.state)

        threads = [threading.Thread(target=setter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 400
        assert all(s == SupervisorState.RUNNING for s in results)


# --- Component Lifecycle ----------------------------------------------------


class TestComponentLifecycle:
    def test_start_component_calls_start(self, supervisor):
        mock = _make_mock_component()
        comp = ComponentInfo("test", lambda: mock, 1)
        assert supervisor._start_component(comp)
        assert comp.is_started
        mock.start.assert_called_once()

    def test_start_component_without_start_method(self, supervisor):
        mock = _make_mock_component(has_start=False)
        comp = ComponentInfo("test", lambda: mock, 1)
        assert supervisor._start_component(comp)
        assert comp.is_started

    def test_start_component_failure_non_critical(self, supervisor):
        def bad_factory():
            raise RuntimeError("boom")

        comp = ComponentInfo("test", bad_factory, 1, is_critical=False)
        assert not supervisor._start_component(comp)
        assert not comp.is_started

    def test_start_component_failure_critical_raises(self, supervisor):
        def bad_factory():
            raise RuntimeError("boom")

        comp = ComponentInfo("test", bad_factory, 1, is_critical=True)
        with pytest.raises(RuntimeError, match="boom"):
            supervisor._start_component(comp)

    def test_stop_component(self, supervisor):
        mock = _make_mock_component()
        comp = ComponentInfo("test", lambda: mock, 1)
        supervisor._start_component(comp)
        supervisor._stop_component(comp)
        assert not comp.is_started
        assert comp.instance is None
        mock.stop.assert_called_once()

    def test_stop_component_not_started(self, supervisor):
        comp = ComponentInfo("test", lambda: MagicMock(), 1)
        supervisor._stop_component(comp)  # should not raise

    def test_stop_component_error_handled(self, supervisor):
        mock = _make_mock_component()
        mock.stop.side_effect = RuntimeError("stop failed")
        comp = ComponentInfo("test", lambda: mock, 1)
        supervisor._start_component(comp)
        supervisor._stop_component(comp)  # should not raise
        assert not comp.is_started

    def test_get_component(self, supervisor):
        mock = _make_mock_component()
        comp = ComponentInfo("test_comp", lambda: mock, 1)
        supervisor._components = [comp]
        supervisor._start_component(comp)
        assert supervisor._get_component("test_comp") is mock
        assert supervisor._get_component("nonexistent") is None

    def test_find_component(self, supervisor):
        comp = ComponentInfo("test_comp", lambda: MagicMock(), 1)
        supervisor._components = [comp]
        assert supervisor._find_component("test_comp") is comp
        assert supervisor._find_component("nonexistent") is None


# --- Start/Stop Order -------------------------------------------------------


class TestStartStopOrder:
    def test_components_start_in_order(self, supervisor):
        order = []
        comps = []
        for i in [3, 1, 2]:
            mock = _make_mock_component()
            mock.start.side_effect = lambda _i=i: order.append(_i)
            comps.append(ComponentInfo(f"c{i}", lambda _m=mock: _m, i))
        supervisor._components = comps
        supervisor._components.sort(key=lambda c: c.start_order)
        supervisor._start_components()
        assert order == [1, 2, 3]

    def test_shutdown_stops_in_reverse(self, supervisor):
        order = []
        comps = []
        for i in [1, 2, 3]:
            mock = _make_mock_component()
            mock.stop.side_effect = lambda _i=i: order.append(_i)
            comps.append(ComponentInfo(f"c{i}", lambda _m=mock: _m, i))
        supervisor._components = sorted(comps, key=lambda c: c.start_order)
        supervisor._start_components()
        supervisor._shutdown()
        assert order == [3, 2, 1]


# --- WiFi / Connection Lost -------------------------------------------------


class TestConnectionLost:
    def test_emergency_stop_connection_lost_triggers_reconnect(self, supervisor):
        supervisor._set_state(SupervisorState.RUNNING)
        supervisor._components = []

        with patch.object(supervisor, "_reconnect") as mock_reconnect:
            event = EmergencyStopEvent(source="connection_lost")
            supervisor._on_emergency_stop(event)
            # Give thread time to start
            time.sleep(0.1)
            mock_reconnect.assert_called_once()

    def test_emergency_stop_cliff_does_not_reconnect(self, supervisor):
        supervisor._set_state(SupervisorState.RUNNING)
        with patch.object(supervisor, "_reconnect") as mock_reconnect:
            event = EmergencyStopEvent(source="cliff")
            supervisor._on_emergency_stop(event)
            time.sleep(0.05)
            mock_reconnect.assert_not_called()

    def test_emergency_stop_ignored_during_shutdown(self, supervisor):
        supervisor._set_state(SupervisorState.SHUTTING_DOWN)
        with patch.object(supervisor, "_reconnect") as mock_reconnect:
            event = EmergencyStopEvent(source="connection_lost")
            supervisor._on_emergency_stop(event)
            time.sleep(0.05)
            mock_reconnect.assert_not_called()

    def test_emergency_stop_ignored_during_reconnect(self, supervisor):
        supervisor._set_state(SupervisorState.RECONNECTING)
        with patch.object(supervisor, "_reconnect") as mock_reconnect:
            event = EmergencyStopEvent(source="connection_lost")
            supervisor._on_emergency_stop(event)
            time.sleep(0.05)
            mock_reconnect.assert_not_called()

    def test_wifi_state_events_emitted(self, supervisor):
        """Verify wifi_state events are emitted during pause/resume."""
        events: list[WifiStateEvent] = []
        supervisor._nuc_bus.on(WIFI_STATE, lambda e: events.append(e))

        # Simulate components
        supervisor._components = []

        supervisor._set_state(SupervisorState.RECONNECTING)
        supervisor._nuc_bus.emit(WIFI_STATE, WifiStateEvent(connected=False))
        supervisor._nuc_bus.emit(WIFI_STATE, WifiStateEvent(connected=True))

        assert len(events) == 2
        assert not events[0].connected
        assert events[1].connected

    def test_pause_stops_connected_components_only(self, supervisor):
        conn_comp = ComponentInfo("connected", lambda: _make_mock_component(), 1)
        local_comp = ComponentInfo(
            "local", lambda: _make_mock_component(), 2, requires_connection=False
        )
        supervisor._components = [conn_comp, local_comp]
        supervisor._start_components()
        assert conn_comp.is_started
        assert local_comp.is_started

        supervisor._pause_connected_components()
        assert not conn_comp.is_started
        assert local_comp.is_started


# --- Health Monitoring ------------------------------------------------------


class TestHealthMonitoring:
    def test_dead_component_restarted(self, supervisor):
        mock = _make_mock_component()
        mock.is_alive.return_value = False
        comp = ComponentInfo("dying", lambda: mock, 1)
        supervisor._components = [comp]
        supervisor._start_component(comp)

        # Create a fresh mock for restart
        fresh = _make_mock_component()
        comp.factory = lambda: fresh

        supervisor._check_component_health()
        # Component should have been restarted
        fresh.start.assert_called_once()

    def test_alive_component_not_restarted(self, supervisor):
        mock = _make_mock_component()
        mock.is_alive.return_value = True
        comp = ComponentInfo("alive", lambda: mock, 1)
        supervisor._components = [comp]
        supervisor._start_component(comp)

        supervisor._check_component_health()
        # start called only once (initial)
        mock.start.assert_called_once()

    def test_not_started_component_ignored(self, supervisor):
        comp = ComponentInfo("stopped", lambda: _make_mock_component(), 1)
        supervisor._components = [comp]
        # Don't start it
        supervisor._check_component_health()  # should not raise


# --- Battery Monitoring -----------------------------------------------------


class TestBatteryMonitoring:
    def test_low_battery_stops_non_essential(self, supervisor):
        supervisor._robot = MagicMock()
        batt = MagicMock()
        batt.battery_level = 1  # LOW
        batt.battery_volts = 3.5
        supervisor._robot.get_battery_state.return_value = batt

        # Set up components
        follow = ComponentInfo("follow_planner", lambda: _make_mock_component(), 15)
        detector = ComponentInfo("person_detector", lambda: _make_mock_component(), 9)
        led = ComponentInfo("led_controller", lambda: _make_mock_component(), 6)
        supervisor._components = [led, detector, follow]
        supervisor._start_components()

        supervisor._check_battery()
        assert supervisor._low_battery
        assert not follow.is_started
        assert not detector.is_started

    def test_battery_recovery_restarts_components(self, supervisor):
        supervisor._robot = MagicMock()
        supervisor._low_battery = True

        batt = MagicMock()
        batt.battery_level = 2  # OK
        batt.battery_volts = 4.0
        supervisor._robot.get_battery_state.return_value = batt

        # Components that were stopped
        follow = ComponentInfo("follow_planner", lambda: _make_mock_component(), 15)
        detector = ComponentInfo("person_detector", lambda: _make_mock_component(), 9)
        led = ComponentInfo("led_controller", lambda: _make_mock_component(), 6)
        supervisor._components = [led, detector, follow]
        # Only start LED (simulating low-battery mode)
        supervisor._start_component(led)

        supervisor._check_battery()
        assert not supervisor._low_battery
        assert detector.is_started
        assert follow.is_started

    def test_battery_error_handled(self, supervisor):
        supervisor._robot = MagicMock()
        supervisor._robot.get_battery_state.side_effect = RuntimeError("no batt")
        supervisor._check_battery()  # should not raise


# --- Signal Handlers --------------------------------------------------------


class TestSignalHandlers:
    def test_signal_handler_sets_shutdown_event(self, supervisor):
        assert not supervisor._shutdown_event.is_set()
        supervisor._signal_handler(signal.SIGTERM, None)
        assert supervisor._shutdown_event.is_set()


# --- sd_notify --------------------------------------------------------------


class TestSdNotify:
    def test_no_notify_socket_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove NOTIFY_SOCKET if present
            os.environ.pop("NOTIFY_SOCKET", None)
            _sd_notify_ready()  # should not raise

    def test_notify_socket_sends_ready(self, tmp_path):
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        try:
            with patch.dict(os.environ, {"NOTIFY_SOCKET": sock_path}):
                _sd_notify_ready()
                data = server.recv(1024)
                assert data == b"READY=1"
        finally:
            server.close()


# --- WifiStateEvent ---------------------------------------------------------


class TestWifiStateEvent:
    def test_connected_event(self):
        e = WifiStateEvent(connected=True, detail="ok")
        assert e.connected
        assert e.detail == "ok"

    def test_disconnected_event(self):
        e = WifiStateEvent(connected=False)
        assert not e.connected
        assert e.detail == ""


# --- Properties -------------------------------------------------------------


class TestProperties:
    def test_nuc_bus(self, supervisor):
        assert isinstance(supervisor.nuc_bus, NucEventBus)

    def test_robot_initially_none(self, supervisor):
        assert supervisor.robot is None

    def test_components_returns_copy(self, supervisor):
        supervisor._components = [
            ComponentInfo("a", lambda: MagicMock(), 1)
        ]
        comps = supervisor.components
        assert len(comps) == 1
        comps.clear()
        assert len(supervisor.components) == 1  # original unchanged
