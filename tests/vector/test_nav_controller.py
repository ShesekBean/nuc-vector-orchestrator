"""Unit tests for NavController state machine and navigation."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.map_store import MapStore
from apps.vector.src.planner.nav_controller import NavConfig, NavController, NavState
from apps.vector.src.planner.visual_slam import VisualSLAM
from apps.vector.src.planner.waypoint_manager import WaypointManager


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def motor():
    mc = MagicMock()
    mc.drive_wheels = MagicMock()
    mc.turn_in_place = MagicMock()
    mc.turn_then_drive = MagicMock()
    mc.emergency_stop = MagicMock()
    return mc


@pytest.fixture()
def head():
    hc = MagicMock()
    hc.set_angle = MagicMock(return_value=10.0)
    return hc


@pytest.fixture()
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def map_store(tmp_dir):
    return MapStore(map_dir=tmp_dir)


@pytest.fixture()
def slam(bus):
    slam = VisualSLAM(bus, grid_size_mm=2000, cell_size_mm=50)
    return slam


@pytest.fixture()
def waypoint_mgr(map_store):
    return WaypointManager(map_store, map_name="test")


@pytest.fixture()
def config():
    return NavConfig(
        arrival_tolerance_mm=100.0,
        drive_speed_mmps=50.0,
        max_replan_attempts=1,
    )


@pytest.fixture()
def nav(slam, motor, head, bus, map_store, waypoint_mgr, config):
    return NavController(slam, motor, head, bus, map_store, waypoint_mgr, config)


class TestNavControllerState:
    def test_initial_state_idle(self, nav):
        assert nav.state == NavState.IDLE

    def test_start_stays_idle(self, nav):
        nav.start("test")
        try:
            assert nav.state == NavState.IDLE
        finally:
            nav.stop()

    def test_stop_returns_to_idle(self, nav):
        nav.start("test")
        nav.stop()
        assert nav.state == NavState.IDLE

    def test_double_start_is_noop(self, nav):
        nav.start("test")
        nav.start("test")  # should not crash
        nav.stop()


class TestNavControllerNavigation:
    def test_navigate_to_unknown_waypoint(self, nav, waypoint_mgr):
        nav.start("test")
        result = nav.navigate_to_waypoint("nonexistent")
        assert result is False
        nav.stop()

    def test_navigate_to_known_waypoint(self, nav, waypoint_mgr):
        nav.start("test")
        waypoint_mgr.save("kitchen", x=500, y=0, theta=0.0)
        result = nav.navigate_to_waypoint("kitchen")
        assert result is True
        # Give thread time to start
        time.sleep(0.1)
        nav.stop()

    def test_cancel_navigation(self, nav, waypoint_mgr):
        nav.start("test")
        waypoint_mgr.save("kitchen", x=500, y=0, theta=0.0)
        nav.navigate_to_waypoint("kitchen")
        time.sleep(0.1)
        nav.cancel_navigation()
        assert nav.state == NavState.IDLE
        nav.stop()

    def test_save_current_position(self, nav, waypoint_mgr):
        nav.start("test")
        result = nav.save_current_position("charger", "Charging dock")
        assert result is True
        wp = waypoint_mgr.get("charger")
        assert wp is not None
        nav.stop()


class TestNavControllerStatus:
    def test_get_status_idle(self, nav):
        nav.start("test")
        status = nav.get_status()
        assert status["state"] == "idle"
        assert "pose" in status
        assert "map" in status
        assert "waypoints" in status
        nav.stop()

    def test_progress_no_nav(self, nav):
        prog = nav.progress
        assert prog["segments_total"] == 0


class TestNavControllerMapping:
    def test_mapping_mode(self, nav):
        nav.start("test")
        nav.start_mapping()
        assert nav.state == NavState.MAPPING
        nav.stop_mapping()
        assert nav.state == NavState.IDLE
        nav.stop()
