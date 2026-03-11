"""Motor control wrappers with cliff-safe differential drive planner.

All motor commands flow through a single ``_safe_drive`` gate that checks
cliff sensors before and during movement.  There is **no** public API that
bypasses cliff safety — ``emergency_stop`` is the only method that writes
to motors without a cliff pre-check (because stopping is always safe).

Cliff sensor layout (4 sensors, bitmask):
  bit 0 — front-left
  bit 1 — front-right
  bit 2 — rear-left
  bit 3 — rear-right

Direction logic:
  forward  → blocked if front-left OR front-right
  reverse  → blocked if rear-left  OR rear-right
  rotation → blocked if ANY sensor triggered
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    CliffTriggeredEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SPEED_MMPS = 200.0
DEFAULT_ACCEL_MMPS2 = 200.0
DEFAULT_TURN_SPEED_DPS = 100.0
DEFAULT_TURN_ACCEL_DPS2 = 200.0
DEFAULT_TURN_TOLERANCE_DEG = 2.0
CLIFF_BACKUP_DIST_MM = 20.0
CLIFF_BACKUP_SPEED_MMPS = 50.0
RAMP_STEP_MS = 20  # acceleration ramp interval

# Cliff sensor bitmask positions
CLIFF_FRONT_LEFT = 0x01
CLIFF_FRONT_RIGHT = 0x02
CLIFF_REAR_LEFT = 0x04
CLIFF_REAR_RIGHT = 0x08
CLIFF_FRONT = CLIFF_FRONT_LEFT | CLIFF_FRONT_RIGHT
CLIFF_REAR = CLIFF_REAR_LEFT | CLIFF_REAR_RIGHT
CLIFF_ANY = CLIFF_FRONT | CLIFF_REAR


class Direction(Enum):
    """Movement direction for cliff-safety checks."""

    FORWARD = auto()
    REVERSE = auto()
    ROTATE = auto()
    STOP = auto()  # always allowed


class CliffSafetyError(Exception):
    """Raised when a movement is blocked by cliff detection."""


# ---------------------------------------------------------------------------
# CliffMonitor — keeps live cliff state from NUC event bus
# ---------------------------------------------------------------------------


class CliffMonitor:
    """Subscribes to cliff events on the NUC bus and maintains current state.

    Thread-safe: ``cliff_flags`` is updated under a lock and can be read
    from any thread.
    """

    def __init__(self, nuc_bus: NucEventBus) -> None:
        self._bus = nuc_bus
        self._lock = threading.Lock()
        self._cliff_flags: int = 0
        self._running = False

    def start(self) -> None:
        """Begin listening for cliff events."""
        if self._running:
            return
        self._bus.on(CLIFF_TRIGGERED, self._on_cliff)
        self._running = True
        logger.info("CliffMonitor started")

    def stop(self) -> None:
        """Stop listening for cliff events."""
        if not self._running:
            return
        self._bus.off(CLIFF_TRIGGERED, self._on_cliff)
        self._running = False
        logger.info("CliffMonitor stopped")

    @property
    def cliff_flags(self) -> int:
        """Current cliff sensor bitmask (0 = all clear)."""
        with self._lock:
            return self._cliff_flags

    def clear(self) -> None:
        """Reset cliff flags (e.g. after backing away from edge)."""
        with self._lock:
            self._cliff_flags = 0

    def is_direction_blocked(self, direction: Direction) -> bool:
        """Check whether *direction* is blocked by cliff sensors."""
        flags = self.cliff_flags
        if direction is Direction.STOP:
            return False  # stopping is always allowed
        if direction is Direction.FORWARD:
            return bool(flags & CLIFF_FRONT)
        if direction is Direction.REVERSE:
            return bool(flags & CLIFF_REAR)
        # ROTATE — any cliff blocks rotation
        return bool(flags & CLIFF_ANY)

    def _on_cliff(self, event: CliffTriggeredEvent) -> None:
        with self._lock:
            self._cliff_flags |= event.cliff_flags
        logger.warning("CliffMonitor updated flags=0x%02x", event.cliff_flags)


# ---------------------------------------------------------------------------
# MotorController
# ---------------------------------------------------------------------------


class MotorController:
    """Cliff-safe motor control for Vector's differential drive.

    Every motor command passes through ``_safe_drive`` which:
    1. Pre-checks cliff sensors for the intended direction.
    2. Executes the command.

    Continuous cliff monitoring is handled by the NUC event bus →
    ``SensorHandler`` → ``emergency_stop`` path (already wired in the
    existing sensor_handler.py).  If a cliff is detected *during* a
    long-running SDK behavior call, the ``SdkEventBridge`` emits
    ``emergency_stop`` which the ``SensorHandler`` handles by zeroing
    the wheels immediately.

    Usage::

        mc = MotorController(robot, nuc_bus)
        mc.start()
        mc.drive_straight(200, 100)   # 200 mm at 100 mm/s
        mc.turn_in_place(90)          # 90° left
        mc.turn_then_drive(45, 300)   # face 45° then drive 300 mm
        mc.emergency_stop()
        mc.stop()
    """

    def __init__(self, robot: Any, nuc_bus: NucEventBus) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._cliff = CliffMonitor(nuc_bus)
        self._stopped = False  # True after emergency_stop until cleared
        self._lock = threading.Lock()

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start cliff monitoring."""
        self._cliff.start()
        self._stopped = False
        logger.info("MotorController started")

    def stop(self) -> None:
        """Stop cliff monitoring and zero motors."""
        self.emergency_stop()
        self._cliff.stop()
        logger.info("MotorController stopped")

    # -- Emergency stop (NO cliff check — always allowed) --------------------

    def emergency_stop(self) -> None:
        """Immediately zero both wheel motors.

        This is the ONLY motor method that bypasses ``_safe_drive``.
        Stopping is always safe.
        """
        with self._lock:
            self._stopped = True
        try:
            self._robot.motors.set_wheel_motors(0.0, 0.0, 0.0, 0.0)
            logger.info("Emergency stop executed")
        except Exception:
            logger.exception("Failed to execute emergency stop")

    def clear_stop(self) -> None:
        """Clear the emergency-stop latch so new commands are accepted."""
        with self._lock:
            self._stopped = False
        self._cliff.clear()

    # -- Raw wheel control ---------------------------------------------------

    def drive_wheels(
        self,
        left_speed: float,
        right_speed: float,
        left_accel: float = DEFAULT_ACCEL_MMPS2,
        right_accel: float = DEFAULT_ACCEL_MMPS2,
    ) -> None:
        """Set individual wheel speeds (mm/s) with cliff safety.

        Positive speeds = forward, negative = reverse.
        """
        left_speed = _clamp(left_speed, -MAX_SPEED_MMPS, MAX_SPEED_MMPS)
        right_speed = _clamp(right_speed, -MAX_SPEED_MMPS, MAX_SPEED_MMPS)

        direction = _infer_direction(left_speed, right_speed)
        self._safe_drive(
            direction,
            lambda: self._robot.motors.set_wheel_motors(
                left_speed, right_speed, left_accel, right_accel
            ),
        )

    # -- Straight line -------------------------------------------------------

    def drive_straight(
        self,
        distance_mm: float,
        speed_mmps: float = MAX_SPEED_MMPS,
    ) -> None:
        """Drive in a straight line for *distance_mm* at *speed_mmps*.

        Positive distance = forward, negative = reverse.
        Speed is always positive (direction from distance sign).
        """
        speed_mmps = min(abs(speed_mmps), MAX_SPEED_MMPS)
        direction = Direction.FORWARD if distance_mm >= 0 else Direction.REVERSE

        def _execute() -> None:
            from anki_vector.util import distance_mm as sdk_dist, speed_mmps as sdk_speed

            self._robot.behavior.drive_straight(
                sdk_dist(distance_mm),
                sdk_speed(speed_mmps),
            )

        self._safe_drive(direction, _execute)

    # -- Turn in place -------------------------------------------------------

    def turn_in_place(
        self,
        angle_deg: float,
        speed_dps: float = DEFAULT_TURN_SPEED_DPS,
        accel_dps2: float = DEFAULT_TURN_ACCEL_DPS2,
        tolerance_deg: float = DEFAULT_TURN_TOLERANCE_DEG,
    ) -> None:
        """Rotate *angle_deg* in place (positive = counter-clockwise)."""

        def _execute() -> None:
            from anki_vector.util import degrees

            self._robot.behavior.turn_in_place(
                degrees(angle_deg),
                speed=degrees(speed_dps),
                accel=degrees(accel_dps2),
                angle_tolerance=degrees(tolerance_deg),
            )

        self._safe_drive(Direction.ROTATE, _execute)

    # -- Turn-then-drive (differential drive planner) ------------------------

    def turn_then_drive(
        self,
        angle_deg: float,
        distance_mm: float,
        drive_speed_mmps: float = MAX_SPEED_MMPS,
        turn_speed_dps: float = DEFAULT_TURN_SPEED_DPS,
    ) -> None:
        """Compound movement: rotate to heading, then drive forward.

        This is the differential-drive replacement for R3's mecanum strafe.
        """
        if angle_deg != 0.0:
            self.turn_in_place(angle_deg, speed_dps=turn_speed_dps)
        if distance_mm != 0.0:
            self.drive_straight(distance_mm, speed_mmps=drive_speed_mmps)

    # -- Speed ramping -------------------------------------------------------

    def ramp_wheels(
        self,
        target_left: float,
        target_right: float,
        ramp_time_ms: int = 200,
    ) -> None:
        """Smoothly ramp wheel speeds from current (assumed 0) to target.

        Issues incremental ``set_wheel_motors`` calls over *ramp_time_ms*.
        """
        target_left = _clamp(target_left, -MAX_SPEED_MMPS, MAX_SPEED_MMPS)
        target_right = _clamp(target_right, -MAX_SPEED_MMPS, MAX_SPEED_MMPS)

        direction = _infer_direction(target_left, target_right)
        steps = max(1, ramp_time_ms // RAMP_STEP_MS)
        step_delay = (ramp_time_ms / 1000.0) / steps

        def _execute() -> None:
            for i in range(1, steps + 1):
                frac = i / steps
                left = target_left * frac
                right = target_right * frac
                # Re-check cliff before each step
                if self._cliff.is_direction_blocked(direction):
                    self.emergency_stop()
                    self._handle_cliff_reaction()
                    raise CliffSafetyError(
                        f"Cliff detected during ramp (step {i}/{steps})"
                    )
                self._robot.motors.set_wheel_motors(
                    left, right, DEFAULT_ACCEL_MMPS2, DEFAULT_ACCEL_MMPS2
                )
                if i < steps:
                    time.sleep(step_delay)

        self._safe_drive(direction, _execute)

    # -- Internal safety gate ------------------------------------------------

    def _safe_drive(self, direction: Direction, action: Any) -> None:
        """Single choke point for ALL motor commands.

        1. Check emergency-stop latch.
        2. Pre-check cliff sensors for *direction*.
        3. Execute *action*.
        """
        with self._lock:
            if self._stopped:
                raise CliffSafetyError(
                    "Motor controller is in emergency-stop state — "
                    "call clear_stop() first"
                )

        if self._cliff.is_direction_blocked(direction):
            logger.warning(
                "Movement blocked by cliff sensor (direction=%s, flags=0x%02x)",
                direction.name,
                self._cliff.cliff_flags,
            )
            self._handle_cliff_reaction()
            raise CliffSafetyError(
                f"Cliff detected — {direction.name} movement blocked"
            )

        action()

    def _handle_cliff_reaction(self) -> None:
        """React to cliff detection: announce + back up slightly.

        Called after emergency stop has already been triggered (either by
        ``_safe_drive`` pre-check or by the NUC bus → SensorHandler path).
        """
        try:
            self._robot.behavior.say_text("Whoa, edge detected")
        except Exception:
            logger.exception("Failed to announce cliff detection")

        # Back up slightly in the safe direction
        cliff_flags = self._cliff.cliff_flags
        if cliff_flags & CLIFF_FRONT and not (cliff_flags & CLIFF_REAR):
            # Front cliff — back up
            try:
                from anki_vector.util import distance_mm, speed_mmps

                self._robot.behavior.drive_straight(
                    distance_mm(-CLIFF_BACKUP_DIST_MM),
                    speed_mmps(CLIFF_BACKUP_SPEED_MMPS),
                )
            except Exception:
                logger.exception("Failed to back up after cliff detection")
        elif cliff_flags & CLIFF_REAR and not (cliff_flags & CLIFF_FRONT):
            # Rear cliff — drive forward slightly
            try:
                from anki_vector.util import distance_mm, speed_mmps

                self._robot.behavior.drive_straight(
                    distance_mm(CLIFF_BACKUP_DIST_MM),
                    speed_mmps(CLIFF_BACKUP_SPEED_MMPS),
                )
            except Exception:
                logger.exception("Failed to move forward after rear cliff")
        # If both front and rear are triggered, don't move at all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _infer_direction(left_speed: float, right_speed: float) -> Direction:
    """Infer movement direction from wheel speeds.

    Pure rotation (opposite sign, similar magnitude) → ROTATE.
    Both positive → FORWARD, both negative → REVERSE.
    Mixed → use the dominant direction.
    """
    if left_speed == 0.0 and right_speed == 0.0:
        return Direction.STOP

    # Check for pure rotation (opposite signs, similar magnitude)
    if left_speed * right_speed < 0:
        ratio = min(abs(left_speed), abs(right_speed)) / max(
            abs(left_speed), abs(right_speed)
        )
        if ratio > 0.5:
            return Direction.ROTATE

    # Net forward/reverse
    avg = (left_speed + right_speed) / 2.0
    if avg > 0:
        return Direction.FORWARD
    if avg < 0:
        return Direction.REVERSE
    return Direction.ROTATE
