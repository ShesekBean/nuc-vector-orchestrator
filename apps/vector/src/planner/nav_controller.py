"""Navigation controller — high-level state machine for waypoint navigation.

Orchestrates the full navigation pipeline:
1. Look up target waypoint
2. Plan A* path on occupancy grid
3. Execute path segments (turn-then-drive)
4. Handle obstacles and replanning
5. Confirm arrival

State machine::

    IDLE ──start──► PLANNING ──path_found──► NAVIGATING
      ▲                │                         │
      │            no_path                  arrived / blocked
      │                │                         │
      └────────────────┴─────────────────────────┘

Passive mapping: during navigation (and follow mode), the SLAM system
continues building the occupancy grid. This means the map improves
every time the robot moves.

Usage::

    nav = NavController(slam, motor, head, nuc_bus, map_store, waypoint_mgr)
    nav.start()
    nav.navigate_to_waypoint("kitchen")
    # ... runs asynchronously, emits NAV_STATE_CHANGED events
    nav.stop()
"""

from __future__ import annotations

import enum
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.motor_controller import MotorController
    from apps.vector.src.planner.map_store import MapStore, Waypoint
    from apps.vector.src.planner.visual_slam import VisualSLAM
    from apps.vector.src.planner.waypoint_manager import WaypointManager

logger = logging.getLogger(__name__)


class NavState(enum.Enum):
    """Navigation controller states."""

    IDLE = "idle"
    PLANNING = "planning"
    NAVIGATING = "navigating"
    ARRIVED = "arrived"
    BLOCKED = "blocked"
    MAPPING = "mapping"  # passive exploration/mapping mode


@dataclass
class NavConfig:
    """Navigation controller configuration."""

    # Arrival tolerance
    arrival_tolerance_mm: float = 150.0

    # Segment execution
    drive_speed_mmps: float = 100.0
    turn_speed_dps: float = 80.0

    # Replanning
    max_replan_attempts: int = 3
    replan_delay_s: float = 1.0

    # Heading alignment at waypoint
    align_heading: bool = True

    # Auto-save map interval during navigation (seconds)
    auto_save_interval_s: float = 60.0

    # SLAM frame processing during navigation
    nav_slam_hz: float = 5.0  # process SLAM frames while navigating


class NavController:
    """High-level navigation controller with path planning and execution.

    Wires together VisualSLAM, PathPlanner, WaypointManager, and
    MotorController to provide waypoint-based navigation.

    Args:
        slam: VisualSLAM instance for pose and map.
        motor: MotorController for movement.
        head: HeadController for looking around.
        nuc_bus: Event bus for state change events.
        map_store: Persistent map storage.
        waypoint_mgr: Named waypoint manager.
        config: Navigation parameters.
    """

    def __init__(
        self,
        slam: VisualSLAM,
        motor: MotorController,
        head: HeadController,
        nuc_bus: NucEventBus,
        map_store: MapStore,
        waypoint_mgr: WaypointManager,
        config: NavConfig | None = None,
    ) -> None:
        self._slam = slam
        self._motor = motor
        self._head = head
        self._bus = nuc_bus
        self._map_store = map_store
        self._waypoint_mgr = waypoint_mgr
        self._cfg = config or NavConfig()

        self._state = NavState.IDLE
        self._state_lock = threading.Lock()
        self._running = False
        self._nav_thread: threading.Thread | None = None

        # Current navigation task
        self._target_waypoint: Waypoint | None = None
        self._current_path: list[tuple[float, float]] | None = None
        self._path_index: int = 0

        # Mapping
        self._active_map_name: str = "default"
        self._last_save_time: float = 0.0

        # Obstacle detector — lazy init on start()
        self._obstacle_detector: Any | None = None

    # -- Properties ----------------------------------------------------------

    @property
    def state(self) -> NavState:
        with self._state_lock:
            return self._state

    @property
    def active_map(self) -> str:
        return self._active_map_name

    @property
    def target_waypoint_name(self) -> str | None:
        wp = self._target_waypoint
        return wp.name if wp else None

    @property
    def progress(self) -> dict:
        """Return navigation progress info."""
        path = self._current_path
        if path is None:
            return {"segments_total": 0, "segments_done": 0, "distance_remaining_mm": 0}

        pose = self._slam.get_pose()
        remaining = 0.0
        for i in range(self._path_index, len(path)):
            if i == self._path_index:
                dx = path[i][0] - pose.x
                dy = path[i][1] - pose.y
            else:
                dx = path[i][0] - path[i - 1][0]
                dy = path[i][1] - path[i - 1][1]
            remaining += math.hypot(dx, dy)

        return {
            "segments_total": len(path),
            "segments_done": self._path_index,
            "distance_remaining_mm": round(remaining),
        }

    # -- Lifecycle -----------------------------------------------------------

    def start(self, map_name: str = "default") -> None:
        """Start the navigation controller and SLAM system."""
        if self._running:
            return

        self._active_map_name = map_name
        self._running = True

        # Start SLAM
        self._slam.start()

        # Start obstacle detector
        try:
            from apps.vector.src.planner.obstacle_detector import ObstacleDetector
            self._obstacle_detector = ObstacleDetector(self._motor, self._bus)
            self._obstacle_detector.start()
            logger.info("ObstacleDetector started for navigation")
        except Exception:
            logger.warning("Failed to start ObstacleDetector", exc_info=True)

        # Try to load existing map
        if self._map_store.exists(map_name):
            self._load_map(map_name)

        self._transition(NavState.IDLE)
        logger.info("NavController started (map='%s')", map_name)

    def stop(self) -> None:
        """Stop navigation and save the map."""
        if not self._running:
            return

        self._running = False

        # Cancel any active navigation
        if self._nav_thread is not None:
            self._nav_thread.join(timeout=5.0)
            self._nav_thread = None

        # Stop motors
        try:
            self._motor.drive_wheels(0, 0)
        except Exception:
            pass

        # Stop obstacle detector
        if self._obstacle_detector:
            self._obstacle_detector.stop()
            self._obstacle_detector = None

        # Save map before stopping
        self._save_map()

        # Stop SLAM
        self._slam.stop()

        self._transition(NavState.IDLE)
        logger.info("NavController stopped")

    # -- Navigation API ------------------------------------------------------

    def navigate_to_waypoint(self, waypoint_name: str) -> bool:
        """Start navigating to a named waypoint.

        Returns True if navigation started, False if waypoint not found
        or navigation already in progress.
        """
        if not self._running:
            logger.warning("NavController not running")
            return False

        state = self.state
        if state in (NavState.NAVIGATING, NavState.PLANNING):
            logger.warning("Navigation already in progress (state=%s)", state.value)
            return False

        waypoint = self._waypoint_mgr.get(waypoint_name)
        if waypoint is None:
            logger.warning("Waypoint '%s' not found", waypoint_name)
            return False

        self._target_waypoint = waypoint
        self._nav_thread = threading.Thread(
            target=self._navigate_task,
            name="nav-controller",
            daemon=True,
        )
        self._nav_thread.start()
        return True

    def navigate_to_position(self, x: float, y: float) -> bool:
        """Navigate to absolute world coordinates (mm)."""
        if not self._running:
            return False
        if self.state in (NavState.NAVIGATING, NavState.PLANNING):
            return False

        from apps.vector.src.planner.map_store import Waypoint
        self._target_waypoint = Waypoint(
            name=f"({x:.0f},{y:.0f})", x=x, y=y, theta=0.0,
        )
        self._nav_thread = threading.Thread(
            target=self._navigate_task,
            name="nav-controller",
            daemon=True,
        )
        self._nav_thread.start()
        return True

    def cancel_navigation(self) -> None:
        """Cancel current navigation."""
        if self.state in (NavState.NAVIGATING, NavState.PLANNING):
            self._running = False  # will cause nav thread to exit
            try:
                self._motor.drive_wheels(0, 0)
            except Exception:
                pass
            # Re-enable for future use
            self._running = True
            self._transition(NavState.IDLE)
            logger.info("Navigation cancelled")

    def save_current_position(self, name: str, description: str = "") -> bool:
        """Save the robot's current position as a named waypoint."""
        pose = self._slam.get_pose()
        return self._waypoint_mgr.save(
            name=name,
            x=pose.x,
            y=pose.y,
            theta=pose.theta,
            description=description,
        )

    def start_mapping(self) -> None:
        """Enter mapping mode — SLAM processes frames, robot can be driven manually."""
        if not self._running:
            self.start(self._active_map_name)
        self._transition(NavState.MAPPING)
        logger.info("Mapping mode started")

    def stop_mapping(self) -> None:
        """Exit mapping mode and save the map."""
        self._save_map()
        self._transition(NavState.IDLE)
        logger.info("Mapping mode stopped, map saved")

    # -- Navigation task (runs in thread) ------------------------------------

    def _navigate_task(self) -> None:
        """Full navigation sequence: plan → execute segments → arrive."""
        target = self._target_waypoint
        if target is None:
            return

        self._transition(NavState.PLANNING)

        for attempt in range(self._cfg.max_replan_attempts):
            if not self._running:
                return

            # Plan path
            path = self._plan_path(target.x, target.y)
            if path is None:
                if attempt < self._cfg.max_replan_attempts - 1:
                    logger.info("Replanning (attempt %d/%d)...",
                                attempt + 2, self._cfg.max_replan_attempts)
                    time.sleep(self._cfg.replan_delay_s)
                    continue
                else:
                    self._transition(NavState.BLOCKED)
                    self._emit_nav_result(False, "No path found")
                    return

            self._current_path = path
            self._path_index = 0
            self._transition(NavState.NAVIGATING)

            # Execute path segments
            success = self._execute_path(path)
            if success:
                # Align heading at waypoint if configured
                if self._cfg.align_heading and target.theta != 0.0:
                    self._align_heading(target.theta)

                self._transition(NavState.ARRIVED)
                self._emit_nav_result(True, f"Arrived at {target.name}")

                # Auto-save map after successful navigation
                self._save_map()
                return

            # Execution failed — replan
            logger.info("Path execution failed, will replan")
            self._transition(NavState.PLANNING)
            time.sleep(self._cfg.replan_delay_s)

        self._transition(NavState.BLOCKED)
        self._emit_nav_result(False, "Max replan attempts exceeded")

    def _plan_path(
        self, goal_x: float, goal_y: float
    ) -> list[tuple[float, float]] | None:
        """Plan a path from current position to goal."""
        from apps.vector.src.planner.path_planner import PathPlanner

        pose = self._slam.get_pose()
        planner = PathPlanner(self._slam.get_grid())
        return planner.plan(pose.x, pose.y, goal_x, goal_y)

    def _execute_path(self, path: list[tuple[float, float]]) -> bool:
        """Execute path segments with turn-then-drive.

        Returns True if all segments completed successfully.
        """
        from apps.vector.src.motor_controller import CliffSafetyError

        for i, (tx, ty) in enumerate(path):
            if not self._running:
                return False

            self._path_index = i
            pose = self._slam.get_pose()

            # Compute bearing and distance to next waypoint
            dx = tx - pose.x
            dy = ty - pose.y
            distance = math.hypot(dx, dy)

            if distance < self._cfg.arrival_tolerance_mm:
                continue  # Already close enough to this waypoint

            # Check obstacle detector — if stuck, try escape
            if self._obstacle_detector:
                if self._obstacle_detector.check_stuck():
                    logger.info("Stuck during navigation — escape triggered")
                    return False  # trigger replan
                scale = self._obstacle_detector.speed_scale
                if scale <= 0.0:
                    logger.warning("Obstacle in danger zone at segment %d — replanning", i)
                    return False
                if scale < 1.0:
                    distance *= scale

            target_bearing = math.atan2(dy, dx)
            turn_angle = _normalise_angle(target_bearing - pose.theta)
            turn_angle_deg = math.degrees(turn_angle)

            logger.info(
                "Nav segment %d/%d: turn %.1f deg, drive %.0f mm to (%.0f, %.0f)",
                i + 1, len(path), turn_angle_deg, distance, tx, ty,
            )

            try:
                self._motor.turn_then_drive(
                    angle_deg=turn_angle_deg,
                    distance_mm=distance,
                    drive_speed_mmps=self._cfg.drive_speed_mmps,
                    turn_speed_dps=self._cfg.turn_speed_dps,
                )
                # Reset stuck detection on successful movement
                if self._obstacle_detector:
                    self._obstacle_detector.reset_stuck()
            except CliffSafetyError:
                logger.warning("Navigation blocked by cliff at segment %d", i)
                return False
            except Exception:
                logger.exception("Motor command failed at segment %d", i)
                return False

            # Periodic map save
            now = time.monotonic()
            if now - self._last_save_time > self._cfg.auto_save_interval_s:
                self._save_map()
                self._last_save_time = now

        # Check final arrival
        pose = self._slam.get_pose()
        target = self._target_waypoint
        if target:
            dist = math.hypot(target.x - pose.x, target.y - pose.y)
            logger.info("Final distance to target: %.0f mm", dist)
            return dist < self._cfg.arrival_tolerance_mm * 2  # relaxed for final check

        return True

    def _align_heading(self, target_theta: float) -> None:
        """Turn to align with the target heading."""
        pose = self._slam.get_pose()
        error = _normalise_angle(target_theta - pose.theta)
        error_deg = math.degrees(error)

        if abs(error_deg) < 5.0:
            return  # Close enough

        logger.info("Aligning heading: %.1f deg correction", error_deg)
        try:
            self._motor.turn_in_place(error_deg, speed_dps=self._cfg.turn_speed_dps)
        except Exception:
            logger.exception("Heading alignment failed")

    # -- Map management ------------------------------------------------------

    def _load_map(self, name: str) -> None:
        """Load a saved map into SLAM."""
        try:
            grid_array, landmarks, waypoints, metadata = self._map_store.load(name)

            # Restore occupancy grid
            slam_grid = self._slam.get_grid()
            if grid_array.shape == slam_grid.grid.shape:
                slam_grid._grid = grid_array
                logger.info("Restored occupancy grid from map '%s'", name)
            else:
                logger.warning(
                    "Grid shape mismatch: saved=%s, current=%s — skipping grid restore",
                    grid_array.shape, slam_grid.grid.shape,
                )

            # Restore waypoints
            for wp in waypoints:
                self._waypoint_mgr.save(
                    name=wp.name, x=wp.x, y=wp.y, theta=wp.theta,
                    description=wp.description,
                )
            logger.info("Loaded %d waypoints from map '%s'", len(waypoints), name)

        except FileNotFoundError:
            logger.info("No saved map '%s' found — starting fresh", name)
        except Exception:
            logger.exception("Failed to load map '%s'", name)

    def _save_map(self) -> None:
        """Save current map to disk."""
        try:
            waypoints = self._waypoint_mgr.list_waypoints()
            self._map_store.save(
                name=self._active_map_name,
                grid=self._slam.get_grid(),
                landmarks=None,  # Skip landmarks for now (large)
                waypoints=waypoints,
                total_frames=self._slam.frames_processed,
                loop_closures=self._slam.loop_closure_count,
            )
            self._last_save_time = time.monotonic()
        except Exception:
            logger.exception("Failed to save map '%s'", self._active_map_name)

    # -- State machine -------------------------------------------------------

    def _transition(self, new_state: NavState) -> None:
        with self._state_lock:
            old = self._state
            if old == new_state:
                return
            self._state = new_state

        logger.info("Nav state: %s -> %s", old.value, new_state.value)

        from apps.vector.src.events.event_types import (
            NAV_STATE_CHANGED,
            NavStateChangedEvent,
        )
        self._bus.emit(
            NAV_STATE_CHANGED,
            NavStateChangedEvent(
                state=new_state.value,
                previous_state=old.value,
                target_waypoint=self._target_waypoint.name if self._target_waypoint else None,
            ),
        )

    def _emit_nav_result(self, success: bool, message: str) -> None:
        """Emit navigation result event."""
        from apps.vector.src.events.event_types import (
            NAV_RESULT,
            NavResultEvent,
        )

        pose = self._slam.get_pose()
        target = self._target_waypoint
        self._bus.emit(
            NAV_RESULT,
            NavResultEvent(
                success=success,
                message=message,
                target_name=target.name if target else "",
                final_x=pose.x,
                final_y=pose.y,
                final_theta=pose.theta,
            ),
        )

    # -- Status --------------------------------------------------------------

    def get_status(self) -> dict:
        """Return full navigation status."""
        pose = self._slam.get_pose()
        slam_grid = self._slam.get_grid()

        status = {
            "state": self.state.value,
            "active_map": self._active_map_name,
            "pose": {
                "x": round(pose.x, 1),
                "y": round(pose.y, 1),
                "theta_deg": round(math.degrees(pose.theta), 1),
            },
            "map": {
                "free_cells": slam_grid.free_cell_count,
                "occupied_cells": slam_grid.occupied_cell_count,
                "grid_dim": slam_grid.grid_dim,
            },
            "slam": {
                "frames_processed": self._slam.frames_processed,
                "landmarks": self._slam.landmark_count,
                "loop_closures": self._slam.loop_closure_count,
            },
            "waypoints": [
                {"name": wp.name, "x": round(wp.x, 1), "y": round(wp.y, 1)}
                for wp in self._waypoint_mgr.list_waypoints()
            ],
        }

        target = self._target_waypoint
        if target:
            dist = math.hypot(target.x - pose.x, target.y - pose.y)
            status["navigation"] = {
                "target": target.name,
                "distance_remaining_mm": round(dist),
                **self.progress,
            }

        return status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_angle(angle: float) -> float:
    """Normalise angle to [-pi, pi]."""
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle
