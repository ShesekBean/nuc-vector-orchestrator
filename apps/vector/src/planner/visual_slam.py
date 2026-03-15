"""Camera-only visual SLAM for Vector navigation.

Replaces R3's LiDAR SLAM with monocular visual odometry using OpenCV ORB
features.  All inference runs on NUC — Vector is a thin gRPC camera/motor
endpoint.

Architecture::

    CameraClient (640×360 BGR) ──► VisualOdometry (ORB features)
                                         │
    MotorController odometry ───────────►│ fuse
                                         ▼
                                    VisualSLAM
                                    ├── Pose2D (x, y, θ)
                                    ├── OccupancyGrid (FREE / OCCUPIED / UNKNOWN)
                                    ├── VisualLandmark map (loop closure)
                                    └── NucEventBus events

    WaypointNavigator ── turn_then_drive ──► MotorController

Key constraint: monocular camera cannot determine absolute scale.  Distance
comes from motor dead reckoning; visual features provide rotation correction
and loop-closure detection.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.motor_controller import MotorController

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_GRID_SIZE_MM = 20000  # 20 m square — covers a full apartment
DEFAULT_CELL_SIZE_MM = 50  # 50 mm per cell (400x400 grid = 160K cells)
DEFAULT_ORB_FEATURES = 500  # ORB keypoints per frame
MIN_FEATURE_MATCHES = 10  # below this, fall back to dead reckoning
LOOP_CLOSURE_MATCH_THRESHOLD = 200  # min matches to declare loop closure (was 30 — too low for Vector's dark camera)
LOOP_CLOSURE_MIN_DISTANCE_MM = 1000  # don't check loop closure if too close (was 500)
LANDMARK_SAMPLE_INTERVAL = 5  # store landmark every N frames
TRACK_WIDTH_MM = 47.0  # distance between Vector's treads


class CellState(IntEnum):
    """Occupancy grid cell states."""

    UNKNOWN = 0
    FREE = 1
    OCCUPIED = 2


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Pose2D:
    """Robot pose in world coordinates.

    x, y in millimetres.  theta in radians (0 = facing +x, CCW positive).
    """

    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0

    def copy(self) -> Pose2D:
        return Pose2D(self.x, self.y, self.theta)


@dataclass(frozen=True)
class VisualLandmark:
    """A visual landmark: ORB descriptors anchored to a world position."""

    x: float  # world mm
    y: float  # world mm
    descriptors: Any  # np.ndarray (N×32 uint8 ORB descriptors)
    frame_id: int


# ---------------------------------------------------------------------------
# Occupancy grid
# ---------------------------------------------------------------------------


class OccupancyGrid:
    """2-D occupancy grid for map building.

    Origin is at grid centre.  Coordinates are in mm.

    Args:
        size_mm: Side length of the square map in mm.
        cell_size_mm: Size of each cell in mm.
    """

    def __init__(
        self,
        size_mm: int = DEFAULT_GRID_SIZE_MM,
        cell_size_mm: int = DEFAULT_CELL_SIZE_MM,
    ) -> None:
        import numpy as np

        self.size_mm = size_mm
        self.cell_size_mm = cell_size_mm
        self.grid_dim = size_mm // cell_size_mm
        # Grid: 0 = unknown, 1 = free, 2 = occupied
        self._grid: np.ndarray = np.zeros(
            (self.grid_dim, self.grid_dim), dtype=np.uint8
        )
        # Origin offset: centre of grid
        self._origin_cells = self.grid_dim // 2

    def world_to_cell(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Convert world mm to grid cell indices (row, col)."""
        col = int(round(x_mm / self.cell_size_mm)) + self._origin_cells
        row = int(round(y_mm / self.cell_size_mm)) + self._origin_cells
        return row, col

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < self.grid_dim and 0 <= col < self.grid_dim

    def set_cell(self, x_mm: float, y_mm: float, state: CellState) -> None:
        """Set cell state at world position."""
        row, col = self.world_to_cell(x_mm, y_mm)
        if self.in_bounds(row, col):
            self._grid[row, col] = int(state)

    def get_cell(self, x_mm: float, y_mm: float) -> CellState:
        """Get cell state at world position."""
        row, col = self.world_to_cell(x_mm, y_mm)
        if not self.in_bounds(row, col):
            return CellState.UNKNOWN
        return CellState(self._grid[row, col])

    def mark_line_free(
        self, x0_mm: float, y0_mm: float, x1_mm: float, y1_mm: float
    ) -> None:
        """Mark all cells along a line as FREE (Bresenham)."""
        r0, c0 = self.world_to_cell(x0_mm, y0_mm)
        r1, c1 = self.world_to_cell(x1_mm, y1_mm)
        for r, c in _bresenham(r0, c0, r1, c1):
            if self.in_bounds(r, c):
                self._grid[r, c] = int(CellState.FREE)

    def mark_fov_free(
        self,
        x_mm: float,
        y_mm: float,
        theta: float,
        max_range_mm: float = 1500.0,
        fov_deg: float = 120.0,
        num_rays: int = 24,
        obstacle_range_mm: float | None = None,
    ) -> None:
        """Cast rays through camera FOV and mark cells as FREE.

        Marks all cells along each ray as FREE up to max_range_mm or
        the obstacle distance. If obstacle_range_mm is provided, marks
        the endpoint as OCCUPIED.

        Args:
            x_mm, y_mm: Robot position in world mm.
            theta: Robot heading in radians.
            max_range_mm: Maximum ray distance.
            fov_deg: Camera field of view.
            num_rays: Number of rays to cast.
            obstacle_range_mm: If set, mark cell at this range as OCCUPIED.
        """
        import math

        half_fov = math.radians(fov_deg / 2)
        for i in range(num_rays):
            angle = theta + (-half_fov + i * 2 * half_fov / max(num_rays - 1, 1))
            ray_range = min(max_range_mm, obstacle_range_mm or max_range_mm)
            end_x = x_mm + ray_range * math.cos(angle)
            end_y = y_mm + ray_range * math.sin(angle)
            self.mark_line_free(x_mm, y_mm, end_x, end_y)

            # Mark obstacle cell if detected
            if obstacle_range_mm is not None and obstacle_range_mm < max_range_mm:
                obs_x = x_mm + obstacle_range_mm * math.cos(angle)
                obs_y = y_mm + obstacle_range_mm * math.sin(angle)
                row, col = self.world_to_cell(obs_x, obs_y)
                if self.in_bounds(row, col):
                    self._grid[row, col] = int(CellState.OCCUPIED)

    @property
    def free_cell_count(self) -> int:
        import numpy as np

        return int(np.sum(self._grid == int(CellState.FREE)))

    @property
    def occupied_cell_count(self) -> int:
        import numpy as np

        return int(np.sum(self._grid == int(CellState.OCCUPIED)))

    @property
    def grid(self) -> np.ndarray:
        """Raw grid array (read-only reference)."""
        return self._grid


# ---------------------------------------------------------------------------
# Visual odometry
# ---------------------------------------------------------------------------


class VisualOdometry:
    """ORB-based visual odometry for monocular camera.

    Extracts ORB features from consecutive frames, matches them, and
    estimates rotation via the essential matrix.  Does NOT estimate
    translation scale (monocular ambiguity) — that comes from motor
    odometry.

    Args:
        n_features: Number of ORB keypoints to detect.
    """

    def __init__(self, n_features: int = DEFAULT_ORB_FEATURES) -> None:
        self._n_features = n_features
        self._orb: Any | None = None  # lazy cv2.ORB_create
        self._bf: Any | None = None  # lazy cv2.BFMatcher
        self._prev_kp: Any | None = None
        self._prev_des: Any | None = None
        self._frame_count = 0
        self._last_process_time = 0.0

    def _ensure_init(self) -> None:
        """Lazy-init ORB detector and BFMatcher."""
        if self._orb is not None:
            return
        import cv2

        self._orb = cv2.ORB_create(nfeatures=self._n_features)
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def process_frame(
        self, frame: np.ndarray
    ) -> tuple[float | None, int, float]:
        """Process a BGR frame and return rotation estimate.

        Returns:
            (delta_theta, match_count, process_time_ms)
            delta_theta is None if insufficient matches (caller should
            fall back to dead reckoning).
        """
        import cv2

        self._ensure_init()
        start = time.monotonic()

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        kp, des = self._orb.detectAndCompute(gray, None)

        delta_theta: float | None = None
        match_count = 0

        if self._prev_des is not None and des is not None and len(des) > 0:
            matches = self._bf.match(self._prev_des, des)
            match_count = len(matches)

            if match_count >= MIN_FEATURE_MATCHES:
                delta_theta = self._estimate_rotation(
                    self._prev_kp, kp, matches
                )

        self._prev_kp = kp
        self._prev_des = des
        self._frame_count += 1
        elapsed_ms = (time.monotonic() - start) * 1000
        self._last_process_time = elapsed_ms

        return delta_theta, match_count, elapsed_ms

    def get_descriptors(self) -> Any | None:
        """Return current frame ORB descriptors (for landmark storage)."""
        return self._prev_des

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def last_process_time_ms(self) -> float:
        return self._last_process_time

    @staticmethod
    def _estimate_rotation(
        kp1: Any, kp2: Any, matches: list[Any]
    ) -> float | None:
        """Estimate rotation between matched keypoint sets via essential matrix.

        Returns delta_theta in radians (CCW positive), or None on failure.
        """
        import cv2
        import numpy as np

        pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
        pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])

        # Camera intrinsics estimate for Vector (640×360, ~120° FOV)
        # fx ≈ w / (2 * tan(FOV/2)) ≈ 640 / (2 * tan(60°)) ≈ 185
        fx = 185.0
        fy = 185.0
        cx = 320.0
        cy = 180.0
        camera_matrix = np.array(
            [[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64
        )

        E, mask = cv2.findEssentialMat(
            pts1, pts2, camera_matrix, method=cv2.RANSAC, threshold=1.0
        )
        if E is None:
            return None

        _, R, _, _ = cv2.recoverPose(E, pts1, pts2, camera_matrix, mask=mask)

        # Extract yaw (rotation about vertical axis) from rotation matrix
        # R is 3×3; yaw = atan2(R[1,0], R[0,0]) for Z-axis rotation
        # but for a ground robot, camera forward = Z axis, so yaw from R:
        yaw = math.atan2(R[1, 0], R[0, 0])
        return yaw


# ---------------------------------------------------------------------------
# Visual SLAM
# ---------------------------------------------------------------------------


class VisualSLAM:
    """Monocular visual SLAM for Vector.

    Fuses visual odometry (rotation from ORB features) with motor dead
    reckoning (distance from wheel commands) to maintain a pose estimate
    and build an occupancy grid.

    Args:
        nuc_bus: NucEventBus for event subscription/emission.
        grid_size_mm: Occupancy grid side length.
        cell_size_mm: Occupancy grid cell size.
        n_features: ORB features per frame.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        grid_size_mm: int = DEFAULT_GRID_SIZE_MM,
        cell_size_mm: int = DEFAULT_CELL_SIZE_MM,
        n_features: int = DEFAULT_ORB_FEATURES,
    ) -> None:
        self._bus = nuc_bus
        self._vo = VisualOdometry(n_features=n_features)
        self._grid = OccupancyGrid(
            size_mm=grid_size_mm, cell_size_mm=cell_size_mm
        )
        self._pose = Pose2D()
        self._landmarks: list[VisualLandmark] = []
        self._lock = threading.Lock()
        self._running = False
        self._frames_processed = 0
        self._loop_closures = 0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to motor and cliff events."""
        if self._running:
            return
        from apps.vector.src.events.event_types import (
            CLIFF_TRIGGERED,
            MOTOR_COMMAND,
        )

        self._bus.on(MOTOR_COMMAND, self._on_motor_command)
        self._bus.on(CLIFF_TRIGGERED, self._on_cliff)
        self._running = True
        logger.info("VisualSLAM started (grid=%dmm, cell=%dmm)",
                     self._grid.size_mm, self._grid.cell_size_mm)

    def stop(self) -> None:
        """Unsubscribe from events."""
        if not self._running:
            return
        from apps.vector.src.events.event_types import (
            CLIFF_TRIGGERED,
            MOTOR_COMMAND,
        )

        self._bus.off(MOTOR_COMMAND, self._on_motor_command)
        self._bus.off(CLIFF_TRIGGERED, self._on_cliff)
        self._running = False
        logger.info(
            "VisualSLAM stopped (frames=%d, landmarks=%d, closures=%d)",
            self._frames_processed,
            len(self._landmarks),
            self._loop_closures,
        )

    # -- Public API ----------------------------------------------------------

    def process_frame(self, frame: np.ndarray) -> Pose2D:
        """Process a camera frame and update pose/map.

        Should be called at camera FPS (~10-15 Hz).

        Returns:
            Current pose estimate.
        """
        with self._lock:
            prev_pose = self._pose.copy()

            # Visual odometry — rotation estimation
            delta_theta, match_count, proc_ms = self._vo.process_frame(frame)

            if delta_theta is not None:
                # Apply visual rotation correction
                self._pose.theta += delta_theta

            # Mark traversed path as free
            self._grid.mark_line_free(
                prev_pose.x, prev_pose.y, self._pose.x, self._pose.y
            )

            # Store visual landmark periodically
            if (
                self._frames_processed % LANDMARK_SAMPLE_INTERVAL == 0
                and self._vo.get_descriptors() is not None
            ):
                self._store_landmark()

            # Check for loop closure
            if len(self._landmarks) > 10:
                self._check_loop_closure()

            self._frames_processed += 1
            pose = self._pose.copy()

        # Emit pose update event (outside lock)
        self._emit_pose_update(pose, match_count, proc_ms)

        return pose

    def get_pose(self) -> Pose2D:
        """Return current pose estimate (thread-safe copy)."""
        with self._lock:
            return self._pose.copy()

    def get_grid(self) -> OccupancyGrid:
        """Return occupancy grid reference."""
        return self._grid

    def update_pose_dead_reckoning(
        self, delta_x: float = 0.0, delta_y: float = 0.0, delta_theta: float = 0.0,
    ) -> None:
        """Apply a dead-reckoning position update.

        Called by the explorer after motor commands to keep the pose
        roughly in sync with reality.  Visual odometry provides rotation
        correction; this provides the translational component that VO
        cannot estimate from pure rotation-based feature matching.
        """
        with self._lock:
            prev = self._pose.copy()
            self._pose.x += delta_x
            self._pose.y += delta_y
            self._pose.theta += delta_theta

            # Mark the traversed path as free
            self._grid.mark_line_free(prev.x, prev.y, self._pose.x, self._pose.y)

    @property
    def frames_processed(self) -> int:
        return self._frames_processed

    @property
    def landmark_count(self) -> int:
        with self._lock:
            return len(self._landmarks)

    @property
    def loop_closure_count(self) -> int:
        return self._loop_closures

    # -- Motor odometry (dead reckoning) -------------------------------------

    def _on_motor_command(self, event: Any) -> None:
        """Update pose from motor command events (dead reckoning).

        MotorCommandEvent has left_speed_mmps, right_speed_mmps, duration_ms.
        """
        with self._lock:
            left = event.left_speed_mmps
            right = event.right_speed_mmps
            dt = event.duration_ms / 1000.0

            if dt <= 0:
                return

            prev_x = self._pose.x
            prev_y = self._pose.y

            # Differential drive kinematics
            if abs(left - right) < 1e-6:
                # Straight line
                dist = left * dt
                self._pose.x += dist * math.cos(self._pose.theta)
                self._pose.y += dist * math.sin(self._pose.theta)
            else:
                # Arc motion
                omega = (right - left) / TRACK_WIDTH_MM
                radius = (left + right) / (2.0 * omega)
                d_theta = omega * dt

                self._pose.x += radius * (
                    math.sin(self._pose.theta + d_theta)
                    - math.sin(self._pose.theta)
                )
                self._pose.y -= radius * (
                    math.cos(self._pose.theta + d_theta)
                    - math.cos(self._pose.theta)
                )
                self._pose.theta += d_theta

            # Normalise theta to [-π, π]
            self._pose.theta = _normalise_angle(self._pose.theta)

            # Mark traversed path as free
            self._grid.mark_line_free(
                prev_x, prev_y, self._pose.x, self._pose.y
            )

    def _on_cliff(self, event: Any) -> None:
        """Mark cells ahead as occupied when cliff sensor triggers."""
        with self._lock:
            # Mark cell ~50mm ahead of current position as occupied
            ahead_dist = 50.0
            cliff_x = self._pose.x + ahead_dist * math.cos(self._pose.theta)
            cliff_y = self._pose.y + ahead_dist * math.sin(self._pose.theta)
            self._grid.set_cell(cliff_x, cliff_y, CellState.OCCUPIED)

    # -- Landmarks and loop closure ------------------------------------------

    def _store_landmark(self) -> None:
        """Store current ORB descriptors as a visual landmark (called under lock)."""
        des = self._vo.get_descriptors()
        if des is None:
            return
        import numpy as np

        lm = VisualLandmark(
            x=self._pose.x,
            y=self._pose.y,
            descriptors=np.copy(des),
            frame_id=self._frames_processed,
        )
        self._landmarks.append(lm)

    def _check_loop_closure(self) -> None:
        """Check if current features match a previous landmark (called under lock)."""
        import cv2

        current_des = self._vo.get_descriptors()
        if current_des is None or len(current_des) == 0:
            return

        if not hasattr(self, "_lc_bf"):
            self._lc_bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

        best_matches = 0
        best_landmark: VisualLandmark | None = None

        # Only check landmarks that are far enough away (avoid matching self)
        for lm in self._landmarks[:-5]:  # skip last 5 (too recent)
            dist = math.hypot(self._pose.x - lm.x, self._pose.y - lm.y)
            if dist < LOOP_CLOSURE_MIN_DISTANCE_MM:
                continue

            try:
                matches = self._lc_bf.match(lm.descriptors, current_des)
                if len(matches) > best_matches:
                    best_matches = len(matches)
                    best_landmark = lm
            except cv2.error:
                continue

        if (
            best_matches >= LOOP_CLOSURE_MATCH_THRESHOLD
            and best_landmark is not None
        ):
            # Correct pose drift towards landmark position
            correction_weight = 0.1  # subtle correction (was 0.3 — too aggressive)
            self._pose.x += correction_weight * (
                best_landmark.x - self._pose.x
            )
            self._pose.y += correction_weight * (
                best_landmark.y - self._pose.y
            )
            self._loop_closures += 1
            logger.info(
                "Loop closure #%d: %d matches with landmark at (%.0f, %.0f), "
                "corrected pose to (%.0f, %.0f)",
                self._loop_closures,
                best_matches,
                best_landmark.x,
                best_landmark.y,
                self._pose.x,
                self._pose.y,
            )

    # -- Events --------------------------------------------------------------

    def _emit_pose_update(
        self, pose: Pose2D, match_count: int, proc_ms: float
    ) -> None:
        """Emit SLAM_POSE_UPDATED event on NucEventBus."""
        from apps.vector.src.events.event_types import (
            SLAM_POSE_UPDATED,
            SlamPoseUpdatedEvent,
        )

        self._bus.emit(
            SLAM_POSE_UPDATED,
            SlamPoseUpdatedEvent(
                x=pose.x,
                y=pose.y,
                theta=pose.theta,
                feature_matches=match_count,
                process_time_ms=proc_ms,
                landmark_count=len(self._landmarks),
                loop_closures=self._loop_closures,
                free_cells=self._grid.free_cell_count,
            ),
        )


# ---------------------------------------------------------------------------
# Waypoint navigator
# ---------------------------------------------------------------------------


class WaypointNavigator:
    """Navigate to world-coordinate waypoints using MotorController.

    Uses turn-then-drive strategy (differential drive — no strafing).

    Args:
        slam: VisualSLAM instance for current pose.
        motor: MotorController for movement commands.
        arrival_tolerance_mm: Distance threshold to consider waypoint reached.
    """

    def __init__(
        self,
        slam: VisualSLAM,
        motor: MotorController,
        arrival_tolerance_mm: float = 100.0,
    ) -> None:
        self._slam = slam
        self._motor = motor
        self._arrival_tolerance_mm = arrival_tolerance_mm

    def navigate_to(
        self,
        target_x: float,
        target_y: float,
        speed_mmps: float = 100.0,
    ) -> bool:
        """Navigate to a world-coordinate target.

        Computes bearing and distance from current pose, then executes
        a turn-then-drive via MotorController.

        Returns:
            True if arrived within tolerance, False if movement was blocked
            (cliff, emergency stop).
        """
        from apps.vector.src.motor_controller import CliffSafetyError

        pose = self._slam.get_pose()
        dx = target_x - pose.x
        dy = target_y - pose.y
        distance = math.hypot(dx, dy)

        if distance <= self._arrival_tolerance_mm:
            logger.info(
                "Already at target (%.0f, %.0f) within tolerance",
                target_x,
                target_y,
            )
            return True

        # Compute bearing to target
        target_bearing = math.atan2(dy, dx)
        turn_angle = _normalise_angle(target_bearing - pose.theta)
        turn_angle_deg = math.degrees(turn_angle)

        # Check occupancy grid for obstacles along path
        grid = self._slam.get_grid()
        target_cell = grid.get_cell(target_x, target_y)
        if target_cell == CellState.OCCUPIED:
            logger.warning(
                "Target (%.0f, %.0f) is in an occupied cell — aborting",
                target_x,
                target_y,
            )
            return False

        try:
            self._motor.turn_then_drive(
                angle_deg=turn_angle_deg,
                distance_mm=distance,
                drive_speed_mmps=speed_mmps,
            )
        except CliffSafetyError:
            logger.warning("Navigation blocked by cliff sensor")
            return False

        # Verify arrival
        new_pose = self._slam.get_pose()
        actual_dist = math.hypot(
            target_x - new_pose.x, target_y - new_pose.y
        )
        arrived = actual_dist <= self._arrival_tolerance_mm
        if arrived:
            logger.info(
                "Arrived at (%.0f, %.0f) (error=%.0fmm)",
                target_x,
                target_y,
                actual_dist,
            )
        else:
            logger.info(
                "Navigate to (%.0f, %.0f): moved but still %.0fmm away",
                target_x,
                target_y,
                actual_dist,
            )
        return arrived


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_angle(angle: float) -> float:
    """Normalise angle to [-π, π]."""
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


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
