"""Unit tests for MapStore and Waypoint persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apps.vector.src.planner.map_store import MapStore, Waypoint, _sanitize_name
from apps.vector.src.planner.visual_slam import OccupancyGrid, CellState


@pytest.fixture()
def tmp_map_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def store(tmp_map_dir):
    return MapStore(map_dir=tmp_map_dir)


@pytest.fixture()
def grid():
    g = OccupancyGrid(size_mm=1000, cell_size_mm=50)
    # Mark some cells
    g.set_cell(100, 100, CellState.FREE)
    g.set_cell(200, 200, CellState.OCCUPIED)
    return g


class TestMapStore:
    def test_save_and_load(self, store, grid):
        waypoints = [
            Waypoint(name="kitchen", x=100, y=200, theta=1.57),
            Waypoint(name="bedroom", x=-300, y=500, theta=0.0),
        ]
        store.save("home", grid, waypoints=waypoints)

        grid_array, landmarks, loaded_wps, metadata = store.load("home")
        assert metadata.name == "home"
        assert len(loaded_wps) == 2
        names = {wp.name for wp in loaded_wps}
        assert "kitchen" in names
        assert "bedroom" in names

    def test_list_maps(self, store, grid):
        store.save("home", grid)
        store.save("office", grid)

        maps = store.list_maps()
        names = [m["name"] for m in maps]
        assert "home" in names
        assert "office" in names

    def test_delete_map(self, store, grid):
        store.save("test", grid)
        assert store.exists("test")
        store.delete_map("test")
        assert not store.exists("test")

    def test_load_nonexistent_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent")

    def test_save_preserves_created_at(self, store, grid):
        store.save("home", grid)
        _, _, _, meta1 = store.load("home")

        store.save("home", grid)
        _, _, _, meta2 = store.load("home")

        assert meta2.created_at == meta1.created_at
        assert meta2.updated_at >= meta1.updated_at

    def test_save_waypoints_only(self, store, grid):
        store.save("home", grid, waypoints=[])
        wps = [Waypoint(name="kitchen", x=100, y=200, theta=0.0)]
        store.save_waypoints("home", wps)

        loaded = store.load_waypoints("home")
        assert len(loaded) == 1
        assert loaded[0].name == "kitchen"


class TestWaypoint:
    def test_to_dict_roundtrip(self):
        wp = Waypoint(name="test", x=100, y=200, theta=1.57, description="A test")
        d = wp.to_dict()
        wp2 = Waypoint.from_dict(d)
        assert wp2.name == wp.name
        assert wp2.x == wp.x
        assert wp2.y == wp.y


class TestSanitizeName:
    def test_basic(self):
        assert _sanitize_name("home") == "home"

    def test_spaces(self):
        assert _sanitize_name("My Kitchen") == "my-kitchen"

    def test_special_chars(self):
        assert _sanitize_name("test@#$!") == "test"

    def test_empty(self):
        assert _sanitize_name("") == "default"
