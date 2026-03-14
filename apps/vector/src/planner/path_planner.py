"""A* path planner on the occupancy grid.

Plans collision-free paths from the robot's current position to a target
waypoint on the occupancy grid.  Uses A* with robot footprint inflation
to ensure the robot can physically traverse the path.

The planner operates on the grid from VisualSLAM's OccupancyGrid and
returns a list of (x_mm, y_mm) waypoints that the NavController follows.

Path smoothing reduces the raw A* cell path to a minimal set of
intermediate waypoints using line-of-sight checks.

Usage::

    planner = PathPlanner(occupancy_grid, inflation_mm=80)
    path = planner.plan(start_x, start_y, goal_x, goal_y)
    # path = [(x1, y1), (x2, y2), ..., (goal_x, goal_y)]
"""

from __future__ import annotations

import heapq
import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.vector.src.planner.visual_slam import OccupancyGrid

logger = logging.getLogger(__name__)

# Robot footprint inflation radius (mm)
# Vector is ~56mm wide, ~100mm long — use 80mm to give clearance
DEFAULT_INFLATION_MM = 80

# Maximum path length in cells before giving up
MAX_PATH_CELLS = 10000


class PathPlanner:
    """A* path planner with robot footprint inflation.

    Args:
        grid: OccupancyGrid from VisualSLAM.
        inflation_mm: Inflate obstacles by this radius for robot clearance.
    """

    def __init__(
        self,
        grid: OccupancyGrid,
        inflation_mm: float = DEFAULT_INFLATION_MM,
    ) -> None:
        self._grid = grid
        self._inflation_mm = inflation_mm
        self._inflated: list[list[bool]] | None = None
        self._inflated_stamp = 0  # track when to rebuild

    def plan(
        self,
        start_x: float,
        start_y: float,
        goal_x: float,
        goal_y: float,
    ) -> list[tuple[float, float]] | None:
        """Plan a path from start to goal in world coordinates (mm).

        Returns:
            List of (x_mm, y_mm) waypoints from start to goal (inclusive),
            or None if no path found.
        """
        grid = self._grid

        # Convert to grid cells
        sr, sc = grid.world_to_cell(start_x, start_y)
        gr, gc = grid.world_to_cell(goal_x, goal_y)

        # Bounds check
        if not grid.in_bounds(sr, sc):
            logger.warning("Start (%.0f, %.0f) is out of grid bounds", start_x, start_y)
            return None
        if not grid.in_bounds(gr, gc):
            logger.warning("Goal (%.0f, %.0f) is out of grid bounds", goal_x, goal_y)
            return None

        # Build inflated obstacle grid
        inflated = self._build_inflated_grid()

        # Check if start or goal is in an inflated obstacle
        if inflated[sr][sc]:
            logger.warning("Start cell (%d, %d) is in inflated obstacle zone", sr, sc)
            # Allow planning from obstacle (robot might be touching a wall)
        if inflated[gr][gc]:
            logger.warning("Goal cell (%d, %d) is in inflated obstacle zone", gr, gc)
            return None

        # A* search
        cell_path = self._astar(sr, sc, gr, gc, inflated)
        if cell_path is None:
            logger.warning(
                "No path found from (%.0f, %.0f) to (%.0f, %.0f)",
                start_x, start_y, goal_x, goal_y,
            )
            return None

        # Convert cell path to world coordinates
        world_path = []
        cell_size = grid.cell_size_mm
        origin = grid.grid_dim // 2
        for r, c in cell_path:
            x = (c - origin) * cell_size
            y = (r - origin) * cell_size
            world_path.append((float(x), float(y)))

        # Smooth path to reduce unnecessary intermediate points
        smoothed = self._smooth_path(world_path, inflated)

        # Ensure the exact goal coordinates are at the end
        if smoothed:
            smoothed[-1] = (goal_x, goal_y)

        logger.info(
            "Path planned: %d raw cells → %d waypoints (%.0fmm total)",
            len(cell_path), len(smoothed),
            _path_length(smoothed),
        )
        return smoothed

    def _build_inflated_grid(self) -> list[list[bool]]:
        """Build a boolean grid with obstacles inflated by robot radius."""
        from apps.vector.src.planner.visual_slam import CellState

        grid = self._grid
        dim = grid.grid_dim
        raw = grid.grid
        inflation_cells = max(1, int(self._inflation_mm / grid.cell_size_mm))

        # Initialize all as passable
        inflated = [[False] * dim for _ in range(dim)]

        # Mark cells within inflation_cells of any occupied cell
        for r in range(dim):
            for c in range(dim):
                if raw[r, c] == int(CellState.OCCUPIED):
                    # Inflate in a square region (fast approximation)
                    for dr in range(-inflation_cells, inflation_cells + 1):
                        for dc in range(-inflation_cells, inflation_cells + 1):
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < dim and 0 <= nc < dim:
                                inflated[nr][nc] = True

        self._inflated = inflated
        return inflated

    def _astar(
        self,
        sr: int, sc: int,
        gr: int, gc: int,
        inflated: list[list[bool]],
    ) -> list[tuple[int, int]] | None:
        """A* search on the grid.

        Returns list of (row, col) from start to goal, or None.
        """
        dim = self._grid.grid_dim

        # Priority queue: (f_score, counter, row, col)
        counter = 0
        open_set: list[tuple[float, int, int, int]] = []
        heapq.heappush(open_set, (_heuristic(sr, sc, gr, gc), counter, sr, sc))

        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g_score: dict[tuple[int, int], float] = {(sr, sc): 0.0}

        # 8-connected neighbors (with diagonal cost sqrt(2))
        neighbors = [
            (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
            (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414),
        ]

        visited = 0

        while open_set:
            _, _, cr, cc = heapq.heappop(open_set)
            visited += 1

            if visited > MAX_PATH_CELLS:
                logger.warning("A* exceeded max cell limit (%d)", MAX_PATH_CELLS)
                return None

            if cr == gr and cc == gc:
                # Reconstruct path
                path = [(cr, cc)]
                while (cr, cc) in came_from:
                    cr, cc = came_from[(cr, cc)]
                    path.append((cr, cc))
                path.reverse()
                return path

            current_g = g_score.get((cr, cc), float("inf"))

            for dr, dc, cost in neighbors:
                nr, nc = cr + dr, cc + dc

                if not (0 <= nr < dim and 0 <= nc < dim):
                    continue
                if inflated[nr][nc]:
                    continue

                tentative_g = current_g + cost

                if tentative_g < g_score.get((nr, nc), float("inf")):
                    came_from[(nr, nc)] = (cr, cc)
                    g_score[(nr, nc)] = tentative_g
                    f = tentative_g + _heuristic(nr, nc, gr, gc)
                    counter += 1
                    heapq.heappush(open_set, (f, counter, nr, nc))

        return None  # No path found

    def _smooth_path(
        self,
        path: list[tuple[float, float]],
        inflated: list[list[bool]],
    ) -> list[tuple[float, float]]:
        """Reduce path to minimal waypoints using line-of-sight checks.

        Greedily skips intermediate points if a straight line between
        two points doesn't cross any inflated obstacles.
        """
        if len(path) <= 2:
            return path

        grid = self._grid
        smoothed = [path[0]]
        i = 0

        while i < len(path) - 1:
            # Find the furthest visible point from current
            furthest = i + 1
            for j in range(len(path) - 1, i, -1):
                if self._line_of_sight(
                    path[i][0], path[i][1],
                    path[j][0], path[j][1],
                    inflated, grid,
                ):
                    furthest = j
                    break

            smoothed.append(path[furthest])
            i = furthest

        return smoothed

    def _line_of_sight(
        self,
        x0: float, y0: float,
        x1: float, y1: float,
        inflated: list[list[bool]],
        grid: OccupancyGrid,
    ) -> bool:
        """Check if a straight line between two world points is obstacle-free."""
        r0, c0 = grid.world_to_cell(x0, y0)
        r1, c1 = grid.world_to_cell(x1, y1)

        for r, c in _bresenham(r0, c0, r1, c1):
            if not grid.in_bounds(r, c):
                return False
            if inflated[r][c]:
                return False
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _heuristic(r1: int, c1: int, r2: int, c2: int) -> float:
    """Octile distance heuristic for 8-connected grid."""
    dr = abs(r1 - r2)
    dc = abs(c1 - c2)
    return max(dr, dc) + 0.414 * min(dr, dc)


def _path_length(path: list[tuple[float, float]]) -> float:
    """Total Euclidean length of a path in mm."""
    total = 0.0
    for i in range(1, len(path)):
        dx = path[i][0] - path[i - 1][0]
        dy = path[i][1] - path[i - 1][1]
        total += math.hypot(dx, dy)
    return total


def _bresenham(
    r0: int, c0: int, r1: int, c1: int
) -> list[tuple[int, int]]:
    """Bresenham's line algorithm for grid cell traversal."""
    cells: list[tuple[int, int]] = []
    dr = abs(r1 - r0)
    dc = abs(c1 - c0)
    sr = 1 if r0 < r1 else -1
    sc = 1 if c0 < c1 else -1
    err = dr - dc

    while True:
        cells.append((r0, c0))
        if r0 == r1 and c0 == c1:
            break
        e2 = 2 * err
        if e2 > -dc:
            err -= dc
            r0 += sr
        if e2 < dr:
            err += dr
            c0 += sc

    return cells
