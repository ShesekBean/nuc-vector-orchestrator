"""Unit tests for AutonomousExplorer and AutoCharger."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.exploration import (
    AutoCharger,
    AutonomousExplorer,
    ExploreConfig,
    ExploreState,
    _get_inbox_size,
)
from apps.vector.src.planner.map_store import MapStore
from apps.vector.src.planner.nav_controller import NavController, NavConfig
from apps.vector.src.planner.visual_slam import CellState, OccupancyGrid, Pose2D, VisualSLAM
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
def camera():
    cam = MagicMock()
    cam.get_latest_frame = MagicMock(return_value=None)
    return cam


@pytest.fixture()
def intercom():
    ic = MagicMock()
    ic.send_text = MagicMock(return_value=True)
    ic.send_photo = MagicMock(return_value=True)
    return ic


@pytest.fixture()
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def slam(bus):
    return VisualSLAM(bus, grid_size_mm=2000, cell_size_mm=50)


@pytest.fixture()
def nav(slam, motor, head, bus, tmp_dir):
    store = MapStore(map_dir=tmp_dir)
    wp_mgr = WaypointManager(store, map_name="test")
    return NavController(
        slam, motor, head, bus, store, wp_mgr,
        NavConfig(arrival_tolerance_mm=100),
    )


@pytest.fixture()
def explorer(slam, motor, head, camera, bus, nav, intercom):
    cfg = ExploreConfig(
        slam_hz=1.0,
        max_explore_time_s=2.0,
        room_check_distance_mm=500,
        step_distance_mm=100,
    )
    return AutonomousExplorer(
        slam, motor, head, camera, bus, nav, intercom, cfg
    )


class TestAutonomousExplorer:
    def test_initial_state(self, explorer):
        assert explorer.state == ExploreState.IDLE

    def test_start_stop(self, explorer, intercom):
        explorer.start()
        # State may be EXPLORING or already IDLE (no frontiers → fast exit)
        assert explorer.state in (ExploreState.EXPLORING, ExploreState.IDLE)
        time.sleep(0.5)
        explorer.stop()
        assert explorer.state == ExploreState.IDLE
        # Should have sent start message at minimum
        assert intercom.send_text.call_count >= 1

    def test_get_status(self, explorer):
        status = explorer.get_status()
        assert "state" in status
        assert "rooms_discovered" in status
        assert "pose" in status
        assert "map" in status

    def test_frontier_detection_empty_grid(self, explorer, slam):
        """Empty grid has no frontiers (nothing is FREE)."""
        frontier = explorer._find_frontier()
        # Grid is all UNKNOWN, no FREE cells border UNKNOWN = no frontier
        assert frontier is None

    def test_frontier_detection_with_free_cells(self, explorer, slam):
        """Grid with FREE cells next to UNKNOWN should have frontiers."""
        grid = slam.get_grid()
        # Mark some cells as free
        for x in range(-200, 200, 50):
            grid.set_cell(x, 0, CellState.FREE)

        frontier = explorer._find_frontier()
        # Should find a frontier at the edge of the free area
        assert frontier is not None

    def test_rooms_discovered_counter(self, explorer):
        assert explorer.rooms_discovered == 0


class TestAutoCharger:
    @pytest.fixture()
    def robot(self):
        r = MagicMock()
        batt = MagicMock()
        batt.battery_volts = 3.9
        batt.battery_level = 2
        batt.is_charging = False
        batt.is_on_charger_platform = False
        r.get_battery_state = MagicMock(return_value=batt)
        r.behavior = MagicMock()
        r.conn = MagicMock()
        return r

    def test_voltage_to_percent(self):
        assert AutoCharger._voltage_to_percent(4.2) == 100.0
        assert AutoCharger._voltage_to_percent(3.3) == 0.0
        pct = AutoCharger._voltage_to_percent(3.7)
        assert 30 < pct < 50

    def test_start_stop(self, robot, nav, bus, intercom):
        charger = AutoCharger(robot, nav, bus, intercom, check_interval_s=60)
        charger.start()
        assert not charger.is_returning
        charger.stop()

    def test_no_charge_when_healthy(self, robot, nav, bus, intercom):
        """Battery at 3.9V (~70%) should not trigger charging."""
        charger = AutoCharger(robot, nav, bus, intercom)
        charger._check_battery()
        assert not charger.is_returning

    def test_charge_triggered_when_low(self, robot, nav, bus, intercom):
        """Battery at 3.5V (~10%) should trigger return to charger."""
        robot.get_battery_state.return_value.battery_volts = 3.5
        charger = AutoCharger(robot, nav, bus, intercom)

        # No charger waypoint saved, so it will try drive_on_charger directly
        charger._check_battery()
        # Should have attempted to dock
        robot.behavior.drive_on_charger.assert_called_once()

    def test_skip_if_already_charging(self, robot, nav, bus, intercom):
        """Don't trigger if already on charger."""
        robot.get_battery_state.return_value.battery_volts = 3.5
        robot.get_battery_state.return_value.is_charging = True
        charger = AutoCharger(robot, nav, bus, intercom)
        charger._check_battery()
        assert not charger.is_returning
