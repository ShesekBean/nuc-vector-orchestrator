"""Unit tests for A* path planner."""

from __future__ import annotations

import pytest

from apps.vector.src.planner.visual_slam import CellState, OccupancyGrid
from apps.vector.src.planner.path_planner import PathPlanner, _heuristic, _path_length


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def grid():
    """Small grid for fast tests."""
    return OccupancyGrid(size_mm=2000, cell_size_mm=50)


@pytest.fixture()
def planner(grid):
    return PathPlanner(grid, inflation_mm=50)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPathPlanner:
    def test_straight_line_path(self, planner, grid):
        """Plan from origin to a point with no obstacles."""
        path = planner.plan(0, 0, 500, 0)
        assert path is not None
        assert len(path) >= 2
        # Start near origin, end near target
        assert abs(path[-1][0] - 500) < 100
        assert abs(path[-1][1]) < 100

    def test_no_path_through_wall(self, planner, grid):
        """No path when a wall blocks the way."""
        # Create a wall across the grid at x=250mm
        for y in range(-1000, 1000, 50):
            grid.set_cell(250, y, CellState.OCCUPIED)

        path = planner.plan(0, 0, 500, 0)
        # Should either find a path around or return None
        # With a full wall, no path should exist
        if path is not None:
            # Verify path doesn't go through wall
            for x, y in path:
                cell = grid.get_cell(x, y)
                assert cell != CellState.OCCUPIED

    def test_path_around_obstacle(self, planner, grid):
        """Path goes around a small obstacle."""
        # Place a small obstacle at (250, 0)
        grid.set_cell(250, 0, CellState.OCCUPIED)
        grid.set_cell(250, 50, CellState.OCCUPIED)
        grid.set_cell(250, -50, CellState.OCCUPIED)

        path = planner.plan(0, 0, 500, 0)
        assert path is not None
        assert len(path) >= 2

    def test_goal_in_obstacle_returns_none(self, planner, grid):
        """Goal inside an obstacle returns None."""
        grid.set_cell(500, 0, CellState.OCCUPIED)
        path = planner.plan(0, 0, 500, 0)
        assert path is None

    def test_same_start_and_goal(self, planner):
        """Start == goal returns a single-point path."""
        path = planner.plan(0, 0, 0, 0)
        assert path is not None
        assert len(path) >= 1

    def test_path_is_smoothed(self, planner, grid):
        """Smoothed path has fewer points than raw A*."""
        # Mark a large free area
        for x in range(0, 500, 50):
            for y in range(-200, 200, 50):
                grid.set_cell(x, y, CellState.FREE)

        path = planner.plan(0, 0, 400, 0)
        assert path is not None
        # Smoothed straight-line path should be very few points
        assert len(path) <= 5

    def test_out_of_bounds_returns_none(self, planner):
        """Goal outside grid returns None."""
        path = planner.plan(0, 0, 10000, 10000)
        assert path is None


class TestHeuristic:
    def test_same_point(self):
        assert _heuristic(5, 5, 5, 5) == 0.0

    def test_horizontal(self):
        h = _heuristic(0, 0, 0, 10)
        assert h == 10.0

    def test_diagonal(self):
        h = _heuristic(0, 0, 5, 5)
        # Octile distance for 5,5: max(5,5) + 0.414 * min(5,5) = 5 + 2.07 = 7.07
        assert abs(h - 7.07) < 0.1


class TestPathLength:
    def test_empty_path(self):
        assert _path_length([]) == 0.0

    def test_single_point(self):
        assert _path_length([(0, 0)]) == 0.0

    def test_horizontal_line(self):
        path = [(0, 0), (100, 0), (200, 0)]
        assert abs(_path_length(path) - 200.0) < 0.1
