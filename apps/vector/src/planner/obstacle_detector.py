"""Camera-based obstacle avoidance for Vector (no LiDAR).

Detects obstacles in the forward camera cone using YOLO bounding boxes,
estimates proximity via bbox area ratio, and provides a ``speed_scale``
factor (0.0–1.0) for the follow planner to throttle motor speed.

Cliff sensors are already handled by ``MotorController._safe_drive()`` —
this module is a **complementary** camera-based soft-slowdown layer.

Detection zones (based on largest obstacle bbox area / frame area):
  - **Danger**  (>= danger_threshold):  speed_scale = 0.0 (full stop)
  - **Caution** (>= caution_threshold): speed_scale linearly 0.3–0.8
  - **Clear**   (< caution_threshold):  speed_scale = 1.0

Escape maneuver:
  Triggered when motor commands are issued but the robot appears stuck
  (no track movement change) for > ``stuck_timeout_s``.  Sequence:
  stop → back up 50 mm → rotate 45° → resume.  Capped at
  ``max_escape_attempts`` to prevent infinite loops.

Usage::

    from apps.vector.src.planner.obstacle_detector import ObstacleDetector

    detector = ObstacleDetector(motor_controller, nuc_bus)
    detector.start()
    # Detection pipeline calls detector.update(detections) each frame
    scale = detector.speed_scale  # follow planner queries this
    detector.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    MOTOR_COMMAND,
    OBSTACLE_DETECTED,
    ObstacleDetectedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.detector.kalman_tracker import Detection
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.motor_controller import MotorController

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_W = 640
FRAME_H = 360
FRAME_AREA = FRAME_W * FRAME_H

# Forward cone: only count obstacles whose center-x falls within this
# fraction of the frame width (centered).  0.6 = middle 60%.
FORWARD_CONE_RATIO = 0.6

# Escape maneuver parameters
ESCAPE_BACKUP_MM = 50.0
ESCAPE_ROTATE_DEG = 45.0
ESCAPE_BACKUP_SPEED = 60.0


@dataclass
class ObstacleConfig:
    """Tunable obstacle avoidance parameters."""

    # --- Area thresholds (fraction of frame area) ---
    danger_threshold: float = 0.25  # >= this → full stop
    caution_threshold: float = 0.10  # >= this → proportional slowdown

    # --- Speed scaling in caution zone ---
    caution_min_scale: float = 0.3  # scale at danger boundary
    caution_max_scale: float = 0.8  # scale at caution boundary

    # --- Detection filtering ---
    min_confidence: float = 0.4  # ignore low-confidence detections
    confirm_frames: int = 3  # consecutive frames before reacting
    forward_cone_ratio: float = FORWARD_CONE_RATIO

    # --- Stuck / escape ---
    stuck_timeout_s: float = 2.0  # seconds of no movement → stuck
    max_escape_attempts: int = 3  # cap escape maneuvers

    # --- Exclusion ---
    person_class: int = 0  # COCO class 0 (person) — excluded from obstacles


# ---------------------------------------------------------------------------
# ObstacleDetector
# ---------------------------------------------------------------------------


class ObstacleDetector:
    """Camera-based obstacle detector with speed scaling and escape maneuver.

    Args:
        motor_controller: Cliff-safe motor controller for escape maneuvers.
        nuc_bus: NUC event bus for pub/sub.
        config: Tunable parameters (optional).
    """

    def __init__(
        self,
        motor_controller: MotorController,
        nuc_bus: NucEventBus,
        config: ObstacleConfig | None = None,
    ) -> None:
        self._motor = motor_controller
        self._bus = nuc_bus
        self._cfg = config or ObstacleConfig()

        # Thread safety
        self._lock = threading.Lock()

        # Obstacle state
        self._speed_scale: float = 1.0
        self._zone: str = "clear"
        self._consecutive_obstacle_frames: int = 0
        self._largest_area_ratio: float = 0.0

        # Stuck detection
        self._last_motor_time: float = 0.0
        self._motor_active: bool = False
        self._stuck_start: float = 0.0
        self._escape_count: int = 0
        self._escaping: bool = False

        self._running = False

    # -- Properties ----------------------------------------------------------

    @property
    def speed_scale(self) -> float:
        """Current speed multiplier (0.0–1.0) based on obstacle proximity."""
        with self._lock:
            return self._speed_scale

    @property
    def zone(self) -> str:
        """Current obstacle zone: 'danger', 'caution', or 'clear'."""
        with self._lock:
            return self._zone

    @property
    def is_stuck(self) -> bool:
        """Whether the robot appears stuck (motors active but no progress)."""
        with self._lock:
            if not self._motor_active or self._stuck_start == 0.0:
                return False
            return (time.monotonic() - self._stuck_start) >= self._cfg.stuck_timeout_s

    @property
    def escape_count(self) -> int:
        """Number of escape maneuvers executed."""
        with self._lock:
            return self._escape_count

    @property
    def config(self) -> ObstacleConfig:
        return self._cfg

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start listening for motor and cliff events."""
        if self._running:
            return
        self._bus.on(MOTOR_COMMAND, self._on_motor_command)
        self._bus.on(CLIFF_TRIGGERED, self._on_cliff)
        self._running = True
        logger.info("ObstacleDetector started")

    def stop(self) -> None:
        """Stop listening and reset state."""
        if not self._running:
            return
        self._bus.off(MOTOR_COMMAND, self._on_motor_command)
        self._bus.off(CLIFF_TRIGGERED, self._on_cliff)
        self._running = False
        with self._lock:
            self._speed_scale = 1.0
            self._zone = "clear"
            self._consecutive_obstacle_frames = 0
            self._motor_active = False
            self._stuck_start = 0.0
            self._escape_count = 0
        logger.info("ObstacleDetector stopped")

    # -- Detection update (called by detection pipeline each frame) ----------

    def update(self, detections: list[Detection]) -> float:
        """Process YOLO detections and update obstacle state.

        Filters out person detections (COCO class 0) — only non-person
        objects count as obstacles.  Uses the largest qualifying bbox
        area ratio to determine zone and speed scale.

        Args:
            detections: YOLO detections from current frame.  Each has
                ``cx``, ``cy``, ``width``, ``height``, ``confidence``,
                and optionally ``class_id``.

        Returns:
            Current ``speed_scale`` (0.0–1.0).
        """
        cfg = self._cfg
        cone_left = FRAME_W * (1.0 - cfg.forward_cone_ratio) / 2.0
        cone_right = FRAME_W - cone_left

        max_area_ratio = 0.0

        for det in detections:
            # Skip persons — they are the follow target, not obstacles
            class_id = getattr(det, "class_id", None)
            if class_id == cfg.person_class:
                continue

            # Confidence filter
            if det.confidence < cfg.min_confidence:
                continue

            # Forward cone filter — obstacle center must be in middle band
            if det.cx < cone_left or det.cx > cone_right:
                continue

            area_ratio = (det.width * det.height) / FRAME_AREA
            if area_ratio > max_area_ratio:
                max_area_ratio = area_ratio

        # Determine zone and scale
        with self._lock:
            if max_area_ratio > 0:
                self._consecutive_obstacle_frames += 1
            else:
                self._consecutive_obstacle_frames = 0

            # Require N consecutive frames before reacting (debounce)
            confirmed = self._consecutive_obstacle_frames >= cfg.confirm_frames

            if confirmed and max_area_ratio >= cfg.danger_threshold:
                zone = "danger"
                scale = 0.0
            elif confirmed and max_area_ratio >= cfg.caution_threshold:
                zone = "caution"
                # Linear interpolation within caution band
                t = (max_area_ratio - cfg.caution_threshold) / (
                    cfg.danger_threshold - cfg.caution_threshold
                )
                scale = cfg.caution_max_scale - t * (
                    cfg.caution_max_scale - cfg.caution_min_scale
                )
            else:
                zone = "clear"
                scale = 1.0

            prev_zone = self._zone
            self._zone = zone
            self._speed_scale = scale
            self._largest_area_ratio = max_area_ratio

        # Emit event on zone change
        if zone != prev_zone and confirmed:
            self._bus.emit(
                OBSTACLE_DETECTED,
                ObstacleDetectedEvent(
                    zone=zone,
                    proximity=max_area_ratio,
                    speed_scale=scale,
                    bbox_area_ratio=max_area_ratio,
                ),
            )
            logger.info(
                "Obstacle zone: %s → %s (area_ratio=%.3f, scale=%.2f)",
                prev_zone,
                zone,
                max_area_ratio,
                scale,
            )

        return scale

    # -- Stuck detection + escape maneuver -----------------------------------

    def check_stuck(self) -> bool:
        """Check if the robot is stuck and trigger escape if needed.

        Should be called periodically (e.g. from the follow planner
        control loop).

        Returns:
            True if an escape maneuver was triggered.
        """
        with self._lock:
            if self._escaping:
                return False
            if not self._motor_active:
                self._stuck_start = 0.0
                return False
            if self._stuck_start == 0.0:
                return False
            elapsed = time.monotonic() - self._stuck_start
            if elapsed < self._cfg.stuck_timeout_s:
                return False
            if self._escape_count >= self._cfg.max_escape_attempts:
                logger.warning(
                    "Max escape attempts (%d) reached — stopping",
                    self._cfg.max_escape_attempts,
                )
                return False
            self._escaping = True

        # Execute escape maneuver (outside lock)
        try:
            self._execute_escape()
        finally:
            with self._lock:
                self._escaping = False
                self._stuck_start = 0.0
                self._motor_active = False

        return True

    def reset_stuck(self) -> None:
        """Reset stuck timer — call when track movement is detected."""
        with self._lock:
            self._stuck_start = 0.0

    def _execute_escape(self) -> None:
        """Back up + rotate to escape obstacle."""
        with self._lock:
            self._escape_count += 1
            attempt = self._escape_count

        logger.info("Escape maneuver #%d: backup + rotate", attempt)

        try:
            self._motor.emergency_stop()
        except Exception:
            logger.exception("Failed to stop before escape")

        try:
            self._motor.clear_stop()
            self._motor.drive_straight(-ESCAPE_BACKUP_MM, ESCAPE_BACKUP_SPEED)
        except Exception:
            logger.exception("Failed to back up during escape")

        try:
            self._motor.turn_in_place(ESCAPE_ROTATE_DEG)
        except Exception:
            logger.exception("Failed to rotate during escape")

    # -- Event callbacks -----------------------------------------------------

    def _on_motor_command(self, event: Any) -> None:
        """Track motor activity for stuck detection."""
        with self._lock:
            left = abs(event.left_speed_mmps)
            right = abs(event.right_speed_mmps)
            if left > 1.0 or right > 1.0:
                if not self._motor_active:
                    self._motor_active = True
                    self._stuck_start = time.monotonic()
                self._last_motor_time = time.monotonic()
            else:
                self._motor_active = False
                self._stuck_start = 0.0

    def _on_cliff(self, _event: Any) -> None:
        """Cliff event contributes to stuck detection — treat as obstacle."""
        with self._lock:
            self._zone = "danger"
            self._speed_scale = 0.0
            self._consecutive_obstacle_frames = self._cfg.confirm_frames
