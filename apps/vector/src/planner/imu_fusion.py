"""IMU-fused heading estimator using complementary filter.

Fuses three heading sources with different strengths:
- **Gyroscope** (SDK accel/gyro): High-frequency, smooth, drifts over time
- **Motor odometry** (wheel speeds): No drift for straight lines, slips on turns
- **Visual odometry** (ORB features): No drift, but noisy and slow (~10Hz)

The complementary filter uses the gyro for fast updates and corrects drift
with visual odometry when available.  Motor odometry provides the primary
distance estimate (monocular camera cannot measure scale).

Heading fusion::

    heading = alpha * (heading + gyro_delta) + (1-alpha) * visual_heading

Where alpha=0.85 trusts gyro for short-term and visual for long-term.

Usage::

    fuser = ImuFusion(nuc_bus)
    fuser.start()
    # ... fuser subscribes to IMU_UPDATE events from SDK
    pose = fuser.get_fused_pose()  # (x, y, theta)
    fuser.stop()
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Complementary filter weight: 0.85 = trust gyro 85% for short-term,
# visual 15% for long-term drift correction
DEFAULT_ALPHA = 0.85

# Gyro noise threshold (deg/s) — ignore readings below this
GYRO_NOISE_THRESHOLD_DPS = 1.5

# IMU polling rate when using SDK robot_state
IMU_POLL_HZ = 50.0


@dataclass
class FusedPose:
    """Fused robot pose from IMU + motor + visual sources."""

    x: float = 0.0  # mm, world frame
    y: float = 0.0  # mm, world frame
    theta: float = 0.0  # radians, CCW positive
    heading_confidence: float = 0.0  # 0-1, based on source agreement
    last_gyro_dps: float = 0.0  # last gyro yaw rate (deg/s)
    last_visual_theta: float | None = None  # last visual heading if available

    def copy(self) -> FusedPose:
        return FusedPose(
            x=self.x, y=self.y, theta=self.theta,
            heading_confidence=self.heading_confidence,
            last_gyro_dps=self.last_gyro_dps,
            last_visual_theta=self.last_visual_theta,
        )


class ImuFusion:
    """Complementary filter fusing IMU gyro + motor odometry + visual odometry.

    Subscribes to IMU_UPDATE events (from SdkEventBridge polling robot.gyro)
    and MOTOR_COMMAND events (from follow planner / nav controller).

    The fused heading is used by VisualSLAM to improve pose estimation.

    Args:
        nuc_bus: Event bus for subscribing to IMU and motor events.
        alpha: Complementary filter weight (0-1). Higher = more gyro trust.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        alpha: float = DEFAULT_ALPHA,
    ) -> None:
        self._bus = nuc_bus
        self._alpha = alpha
        self._pose = FusedPose()
        self._lock = threading.Lock()
        self._running = False
        self._last_imu_time: float | None = None

        # Track motor odometry for position
        self._track_width_mm = 47.0  # Vector tread spacing

        # Statistics
        self._imu_updates = 0
        self._motor_updates = 0
        self._visual_corrections = 0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to IMU and motor events."""
        if self._running:
            return
        from apps.vector.src.events.event_types import IMU_UPDATE, MOTOR_COMMAND
        self._bus.on(IMU_UPDATE, self._on_imu_update)
        self._bus.on(MOTOR_COMMAND, self._on_motor_command)
        self._running = True
        self._last_imu_time = None
        logger.info("ImuFusion started (alpha=%.2f)", self._alpha)

    def stop(self) -> None:
        """Unsubscribe from events."""
        if not self._running:
            return
        from apps.vector.src.events.event_types import IMU_UPDATE, MOTOR_COMMAND
        self._bus.off(IMU_UPDATE, self._on_imu_update)
        self._bus.off(MOTOR_COMMAND, self._on_motor_command)
        self._running = False
        logger.info(
            "ImuFusion stopped (imu=%d, motor=%d, visual=%d)",
            self._imu_updates, self._motor_updates, self._visual_corrections,
        )

    # -- Public API ----------------------------------------------------------

    def get_fused_pose(self) -> FusedPose:
        """Return current fused pose (thread-safe copy)."""
        with self._lock:
            return self._pose.copy()

    def apply_visual_correction(self, visual_theta: float) -> None:
        """Apply visual odometry heading correction.

        Called by VisualSLAM when ORB rotation estimate is available.
        The complementary filter blends this with gyro heading.
        """
        with self._lock:
            # Complementary filter: blend gyro (high-freq) with visual (low-freq)
            # heading = alpha * gyro_heading + (1-alpha) * visual_heading
            error = _normalise_angle(visual_theta - self._pose.theta)
            correction = (1.0 - self._alpha) * error
            self._pose.theta = _normalise_angle(self._pose.theta + correction)
            self._pose.last_visual_theta = visual_theta
            self._visual_corrections += 1

            # Update heading confidence based on gyro-visual agreement
            if abs(error) < math.radians(5):
                self._pose.heading_confidence = min(1.0, self._pose.heading_confidence + 0.1)
            else:
                self._pose.heading_confidence = max(0.0, self._pose.heading_confidence - 0.2)

    def reset_pose(self, x: float = 0.0, y: float = 0.0, theta: float = 0.0) -> None:
        """Reset fused pose to given values."""
        with self._lock:
            self._pose = FusedPose(x=x, y=y, theta=theta)
            self._last_imu_time = None

    @property
    def imu_update_count(self) -> int:
        return self._imu_updates

    @property
    def motor_update_count(self) -> int:
        return self._motor_updates

    @property
    def visual_correction_count(self) -> int:
        return self._visual_corrections

    # -- Event handlers ------------------------------------------------------

    def _on_imu_update(self, event: Any) -> None:
        """Process IMU gyro data for heading estimation.

        ImuUpdateEvent has: gyro_x, gyro_y, gyro_z (deg/s),
        accel_x, accel_y, accel_z (G).
        """
        now = time.monotonic()
        with self._lock:
            if self._last_imu_time is None:
                self._last_imu_time = now
                self._imu_updates += 1
                return

            dt = now - self._last_imu_time
            self._last_imu_time = now

            if dt <= 0 or dt > 0.5:  # skip if too large (stale)
                return

            # Gyro Z = yaw rate in deg/s (Vector SDK convention)
            gyro_z_dps = event.gyro_z
            self._pose.last_gyro_dps = gyro_z_dps

            # Apply noise threshold
            if abs(gyro_z_dps) < GYRO_NOISE_THRESHOLD_DPS:
                self._imu_updates += 1
                return

            # Integrate gyro: theta += omega * dt
            # Negative because SDK gyro is CW-positive, we use CCW-positive
            delta_theta = math.radians(-gyro_z_dps) * dt
            self._pose.theta = _normalise_angle(self._pose.theta + delta_theta)

            self._imu_updates += 1

    def _on_motor_command(self, event: Any) -> None:
        """Update position from motor commands (dead reckoning).

        Uses differential drive kinematics for position but NOT heading
        (heading comes from IMU fusion instead).
        """
        with self._lock:
            left = event.left_speed_mmps
            right = event.right_speed_mmps
            dt = event.duration_ms / 1000.0

            if dt <= 0:
                return

            # Use current fused heading for position update
            theta = self._pose.theta

            # Differential drive position update (heading from IMU, not wheels)
            avg_speed = (left + right) / 2.0
            dist = avg_speed * dt
            self._pose.x += dist * math.cos(theta)
            self._pose.y += dist * math.sin(theta)

            self._motor_updates += 1


# ---------------------------------------------------------------------------
# IMU poller — extracts gyro/accel from SDK robot_state
# ---------------------------------------------------------------------------


class ImuPoller:
    """Polls Vector SDK for IMU data and emits IMU_UPDATE events.

    The SDK provides robot.accel (3-axis in G) and robot.gyro (3-axis in deg/s)
    as properties that are updated via the robot_state gRPC stream.

    This poller reads them at IMU_POLL_HZ and emits events on the NUC bus.
    """

    def __init__(
        self,
        robot: Any,
        nuc_bus: NucEventBus,
        poll_hz: float = IMU_POLL_HZ,
    ) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._poll_hz = poll_hz
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start IMU polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name="imu-poller", daemon=True
        )
        self._thread.start()
        logger.info("ImuPoller started at %.0f Hz", self._poll_hz)

    def stop(self) -> None:
        """Stop IMU polling."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("ImuPoller stopped")

    def _poll_loop(self) -> None:
        """Poll SDK robot.accel/gyro and emit IMU_UPDATE events."""
        from apps.vector.src.events.event_types import IMU_UPDATE, ImuUpdateEvent

        period = 1.0 / self._poll_hz

        while self._running:
            loop_start = time.monotonic()

            try:
                accel = self._robot.accel
                gyro = self._robot.gyro

                event = ImuUpdateEvent(
                    accel_x=accel.x,
                    accel_y=accel.y,
                    accel_z=accel.z,
                    gyro_x=gyro.x,
                    gyro_y=gyro.y,
                    gyro_z=gyro.z,
                )
                self._bus.emit(IMU_UPDATE, event)
            except Exception:
                # SDK might be disconnected; skip silently
                pass

            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


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
