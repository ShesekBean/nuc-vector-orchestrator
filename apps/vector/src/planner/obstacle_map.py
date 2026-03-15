"""Shared ObstacleMap — fuses all obstacle detection tiers into unified state.

Aggregates readings from 5 sources:
1. Floor-line proximity detector (Tier 1, ~5ms, every frame)
2. YOLO object detection (Tier 2, ~47ms, 15Hz)
3. Claude Vision LLM (Tier 3, ~1-3s, async background)
4. Cliff sensors (hardware, instant)
5. IMU (collision/tilt detection from accelerometer)

All movement systems (explorer, follow, nav) query the obstacle map
for a unified assessment instead of running their own detection.

Thread-safe: multiple writers (detection threads) + multiple readers
(movement control loops).

Usage::

    obstacle_map = ObstacleMap(nuc_bus)
    obstacle_map.start()

    # Writers (detection threads)
    obstacle_map.update_proximity(reading)
    obstacle_map.update_yolo(detections, speed_scale, zone)
    obstacle_map.update_vision(blocked, direction, description)
    obstacle_map.update_cliff(triggered, sensor_id)
    obstacle_map.update_imu(collision, tilt_excessive)

    # Readers (movement loops)
    assessment = obstacle_map.get_assessment()
    if assessment.zone == "danger":
        turn(assessment.turn_direction)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.vector.src.detector.floor_proximity import ProximityReading
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Vision results expire after this many seconds
VISION_TTL_S = 4.0

# Cliff events expire after this many seconds (cliff is instantaneous)
CLIFF_TTL_S = 2.0

# IMU collision events expire
IMU_COLLISION_TTL_S = 3.0


@dataclass
class ObstacleAssessment:
    """Unified obstacle assessment from all tiers."""

    zone: str = "clear"               # "clear", "caution", "danger"
    speed_scale: float = 1.0          # 0.0 (stop) to 1.0 (full speed)
    turn_direction: str = ""           # "left", "right", or "" (no turn needed)
    source: str = ""                   # which tier triggered the assessment

    # Per-tier details
    proximity_mm: float = 1500.0       # Tier 1: nearest obstacle distance
    yolo_zone: str = "clear"           # Tier 2: YOLO-based zone
    vision_blocked: bool = False       # Tier 3: LLM says blocked
    vision_direction: str = ""         # Tier 3: LLM suggested direction
    vision_description: str = ""       # Tier 3: what the LLM saw
    vision_age_s: float = 999.0        # seconds since last vision check
    cliff_triggered: bool = False      # Tier 4: cliff sensor active
    imu_collision: bool = False        # Tier 5: IMU detected collision


class ObstacleMap:
    """Thread-safe shared obstacle state fusing all detection tiers.

    Args:
        nuc_bus: Event bus for subscribing to cliff/IMU events.
    """

    def __init__(self, nuc_bus: NucEventBus | None = None) -> None:
        self._bus = nuc_bus
        self._lock = threading.Lock()

        # Tier 1: Floor proximity
        self._proximity_mm: float = 1500.0
        self._proximity_turn: str = ""
        self._proximity_confidence: float = 0.0
        self._proximity_time: float = 0.0

        # Tier 2: YOLO
        self._yolo_zone: str = "clear"
        self._yolo_scale: float = 1.0
        self._yolo_time: float = 0.0

        # Tier 3: Vision LLM
        self._vision_blocked: bool = False
        self._vision_direction: str = ""
        self._vision_description: str = ""
        self._vision_time: float = 0.0

        # Tier 4: Cliff sensors
        self._cliff_triggered: bool = False
        self._cliff_time: float = 0.0

        # Tier 5: IMU
        self._imu_collision: bool = False
        self._imu_tilt: bool = False
        self._imu_time: float = 0.0

        self._running = False

    def start(self) -> None:
        """Subscribe to cliff and IMU events on the bus."""
        if self._running:
            return
        self._running = True
        if self._bus:
            from apps.vector.src.events.event_types import CLIFF_TRIGGERED, IMU_UPDATE
            self._bus.on(CLIFF_TRIGGERED, self._on_cliff)
            self._bus.on(IMU_UPDATE, self._on_imu)
        logger.info("ObstacleMap started")

    def stop(self) -> None:
        """Unsubscribe from events."""
        if not self._running:
            return
        self._running = False
        if self._bus:
            from apps.vector.src.events.event_types import CLIFF_TRIGGERED, IMU_UPDATE
            self._bus.off(CLIFF_TRIGGERED, self._on_cliff)
            self._bus.off(IMU_UPDATE, self._on_imu)
        logger.info("ObstacleMap stopped")

    # -- Writers (called by detection threads) --------------------------------

    def update_proximity(self, reading: ProximityReading) -> None:
        """Update from Tier 1 floor-line proximity detector."""
        with self._lock:
            self._proximity_mm = reading.min_mm
            self._proximity_turn = reading.suggested_turn
            self._proximity_confidence = reading.confidence
            self._proximity_time = time.monotonic()

    def update_yolo(self, zone: str, speed_scale: float) -> None:
        """Update from Tier 2 YOLO obstacle detector."""
        with self._lock:
            self._yolo_zone = zone
            self._yolo_scale = speed_scale
            self._yolo_time = time.monotonic()

    def update_vision(
        self, blocked: bool, direction: str = "", description: str = ""
    ) -> None:
        """Update from Tier 3 Claude Vision check."""
        with self._lock:
            self._vision_blocked = blocked
            self._vision_direction = direction
            self._vision_description = description
            self._vision_time = time.monotonic()
        if blocked:
            logger.info("Vision: BLOCKED (%s) — %s", direction, description)

    def update_cliff(self, triggered: bool) -> None:
        """Update from Tier 4 cliff sensor."""
        with self._lock:
            self._cliff_triggered = triggered
            self._cliff_time = time.monotonic()

    def update_imu(self, collision: bool = False, tilt_excessive: bool = False) -> None:
        """Update from Tier 5 IMU accelerometer."""
        with self._lock:
            self._imu_collision = collision
            self._imu_tilt = tilt_excessive
            self._imu_time = time.monotonic()

    # -- Reader (called by movement control loops) ----------------------------

    def get_assessment(self) -> ObstacleAssessment:
        """Get unified obstacle assessment (most restrictive wins)."""
        now = time.monotonic()

        with self._lock:
            # Start with clear
            zone = "clear"
            speed_scale = 1.0
            turn_direction = ""
            source = ""

            # Tier 4: Cliff (highest priority — hardware safety)
            cliff_active = (
                self._cliff_triggered
                and (now - self._cliff_time) < CLIFF_TTL_S
            )
            if cliff_active:
                zone = "danger"
                speed_scale = 0.0
                source = "cliff"

            # Tier 5: IMU collision — informational only, logged but does not
            # override zone. Vector's treads produce too much vibration for
            # reliable collision detection via accelerometer alone.
            imu_collision = (
                self._imu_collision
                and (now - self._imu_time) < IMU_COLLISION_TTL_S
            )

            # Tier 1: Floor proximity
            if self._proximity_mm < 100 and self._proximity_confidence > 0.3:
                zone = "danger"
                speed_scale = 0.0
                turn_direction = self._proximity_turn
                source = source or "proximity"
            elif self._proximity_mm < 250 and self._proximity_confidence > 0.3:
                if zone != "danger":
                    zone = "caution"
                    speed_scale = min(speed_scale, self._proximity_mm / 250.0)
                    turn_direction = turn_direction or self._proximity_turn
                    source = source or "proximity"

            # Tier 2: YOLO
            if self._yolo_zone == "danger":
                zone = "danger"
                speed_scale = 0.0
                source = source or "yolo"
            elif self._yolo_zone == "caution" and zone != "danger":
                zone = "caution"
                speed_scale = min(speed_scale, self._yolo_scale)
                source = source or "yolo"

            # Tier 3: Vision (with TTL) — caution only, never danger.
            # Claude Vision can't judge distance reliably from monocular camera.
            # It flags objects 1-2m away as "blocked". Use it to slow down, not stop.
            vision_age = now - self._vision_time if self._vision_time > 0 else 999.0
            vision_valid = vision_age < VISION_TTL_S
            if vision_valid and self._vision_blocked and zone != "danger":
                zone = "caution"
                speed_scale = min(speed_scale, 0.5)
                turn_direction = turn_direction or self._vision_direction
                source = source or "vision"

            return ObstacleAssessment(
                zone=zone,
                speed_scale=speed_scale,
                turn_direction=turn_direction,
                source=source,
                proximity_mm=self._proximity_mm,
                yolo_zone=self._yolo_zone,
                vision_blocked=self._vision_blocked if vision_valid else False,
                vision_direction=self._vision_direction if vision_valid else "",
                vision_description=self._vision_description if vision_valid else "",
                vision_age_s=round(vision_age, 1),
                cliff_triggered=cliff_active,
                imu_collision=imu_collision,
            )

    @property
    def vision_stale(self) -> bool:
        """True if vision check is stale (older than TTL)."""
        with self._lock:
            if self._vision_time == 0:
                return True
            return (time.monotonic() - self._vision_time) > VISION_TTL_S

    # -- Event handlers -------------------------------------------------------

    def _on_cliff(self, event: Any) -> None:
        """Handle cliff sensor event."""
        self.update_cliff(True)
        logger.info("ObstacleMap: cliff triggered")

    def _on_imu(self, event: Any) -> None:
        """Handle IMU update — check for collision (sudden deceleration).

        Vector's treads produce significant vibration during normal driving
        (easily 3-5G spikes). Only trigger on sustained high lateral accel
        that indicates actual wall contact.
        """
        accel_x = getattr(event, "accel_x", 0.0)
        accel_y = getattr(event, "accel_y", 0.0)
        accel_z = getattr(event, "accel_z", 0.0)

        import math
        lateral_g = math.hypot(accel_x, accel_y)

        # Track consecutive high-G samples to filter tread vibration
        if lateral_g > 8.0:
            self._imu_high_g_count = getattr(self, "_imu_high_g_count", 0) + 1
        else:
            self._imu_high_g_count = 0

        # Collision: 3+ consecutive samples above 8G (real impact, not vibration)
        collision = self._imu_high_g_count >= 3

        # Excessive tilt: Z acceleration significantly different from 1G
        # (robot picked up or fallen over)
        tilt = abs(accel_z) < 0.3 or abs(accel_z) > 1.8

        if collision or tilt:
            self.update_imu(collision=collision, tilt_excessive=tilt)
