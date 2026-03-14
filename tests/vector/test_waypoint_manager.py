"""Unit tests for WaypointManager."""

from __future__ import annotations

import tempfile

import pytest

from apps.vector.src.planner.map_store import MapStore, Waypoint
from apps.vector.src.planner.waypoint_manager import WaypointManager
from apps.vector.src.planner.visual_slam import OccupancyGrid


@pytest.fixture()
def tmp_map_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def store(tmp_map_dir):
    return MapStore(map_dir=tmp_map_dir)


@pytest.fixture()
def mgr(store):
    return WaypointManager(store, map_name="test")


class TestWaypointManager:
    def test_save_and_get(self, mgr):
        mgr.save("kitchen", x=100, y=200, theta=1.57)
        wp = mgr.get("kitchen")
        assert wp is not None
        assert wp.name == "kitchen"
        assert wp.x == 100
        assert wp.y == 200

    def test_case_insensitive(self, mgr):
        mgr.save("Kitchen", x=100, y=200)
        assert mgr.get("kitchen") is not None
        assert mgr.get("KITCHEN") is not None

    def test_delete(self, mgr):
        mgr.save("kitchen", x=100, y=200)
        assert mgr.delete("kitchen")
        assert mgr.get("kitchen") is None
        assert not mgr.delete("kitchen")

    def test_list_sorted(self, mgr):
        mgr.save("kitchen", x=100, y=200)
        mgr.save("bedroom", x=300, y=400)
        mgr.save("attic", x=500, y=600)

        wps = mgr.list_waypoints()
        names = [wp.name for wp in wps]
        assert names == ["attic", "bedroom", "kitchen"]

    def test_nearest(self, mgr):
        mgr.save("kitchen", x=100, y=200)
        mgr.save("bedroom", x=1000, y=2000)

        nearest = mgr.nearest(x=150, y=250)
        assert nearest is not None
        assert nearest.name == "kitchen"

    def test_nearest_empty(self, mgr):
        assert mgr.nearest(0, 0) is None

    def test_distance_to(self, mgr):
        mgr.save("kitchen", x=300, y=400)
        dist = mgr.distance_to("kitchen", 0, 0)
        assert dist is not None
        assert abs(dist - 500) < 1.0  # 3-4-5 triangle

    def test_count(self, mgr):
        assert mgr.count == 0
        mgr.save("a", x=0, y=0)
        mgr.save("b", x=0, y=0)
        assert mgr.count == 2

    def test_empty_name_rejected(self, mgr):
        assert not mgr.save("", x=0, y=0)

    def test_update_existing(self, mgr):
        mgr.save("kitchen", x=100, y=200)
        mgr.save("kitchen", x=300, y=400)

        wp = mgr.get("kitchen")
        assert wp.x == 300
        assert wp.y == 400
        assert mgr.count == 1

    def test_persistence(self, store):
        """Waypoints survive re-instantiation."""
        # Create map on disk first
        grid = OccupancyGrid(size_mm=1000, cell_size_mm=50)
        store.save("test", grid, waypoints=[
            Waypoint(name="kitchen", x=100, y=200, theta=0.0),
        ])

        mgr1 = WaypointManager(store, map_name="test")
        mgr1.save("bedroom", x=300, y=400)

        mgr2 = WaypointManager(store, map_name="test")
        assert mgr2.get("kitchen") is not None
        assert mgr2.get("bedroom") is not None
