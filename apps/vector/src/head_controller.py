"""Head servo controller for Vector.

Wraps Vector SDK ``set_head_angle`` with safety clamping, neutral position,
and configurable speed. All angles in degrees: -22° (full down) to 45° (full up).

Usage::

    from apps.vector.src.head_controller import HeadController

    ctrl = HeadController(robot)
    ctrl.set_angle(20)          # look slightly up
    ctrl.neutral()              # return to neutral (10°)
    ctrl.look_up(speed_dps=60)  # look fully up, slowly
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Vector head angle limits (degrees)
MIN_ANGLE = -22.0
MAX_ANGLE = 45.0
NEUTRAL_ANGLE = 10.0

# Default head speed (degrees per second). SDK default is ~120 dps.
DEFAULT_SPEED_DPS = 120.0


class HeadController:
    """Controls Vector's head servo via the SDK.

    Parameters
    ----------
    robot : anki_vector.Robot
        Connected Vector robot instance.
    default_speed_dps : float
        Default speed for head movements (degrees per second).
    """

    def __init__(self, robot: Any, default_speed_dps: float = DEFAULT_SPEED_DPS) -> None:
        self._robot = robot
        self._default_speed_dps = max(1.0, default_speed_dps)
        self._last_angle: float | None = None

    @staticmethod
    def clamp(angle: float) -> float:
        """Clamp angle to Vector's valid head range [-22, 45] degrees."""
        clamped = max(MIN_ANGLE, min(MAX_ANGLE, angle))
        if clamped != angle:
            logger.warning(
                "Head angle %.1f° clamped to %.1f° (range [%.0f, %.0f])",
                angle, clamped, MIN_ANGLE, MAX_ANGLE,
            )
        return clamped

    def set_angle(self, angle_deg: float, speed_dps: float | None = None) -> float:
        """Move head to an absolute angle.

        Parameters
        ----------
        angle_deg : float
            Target angle in degrees. Clamped to [-22, 45].
        speed_dps : float | None
            Speed in degrees per second. Uses default if None.

        Returns
        -------
        float
            The actual (clamped) angle that was commanded.
        """
        from anki_vector.util import degrees

        target = self.clamp(angle_deg)
        spd = speed_dps if speed_dps is not None else self._default_speed_dps
        spd = max(1.0, spd)

        self._robot.behavior.set_head_angle(
            degrees(target), max_speed=spd
        )
        self._last_angle = target
        logger.debug("Head → %.1f° (speed=%.0f dps)", target, spd)
        return target

    def neutral(self, speed_dps: float | None = None) -> float:
        """Move head to neutral position (10°)."""
        return self.set_angle(NEUTRAL_ANGLE, speed_dps=speed_dps)

    def look_up(self, speed_dps: float | None = None) -> float:
        """Move head to maximum up angle (45°)."""
        return self.set_angle(MAX_ANGLE, speed_dps=speed_dps)

    def look_down(self, speed_dps: float | None = None) -> float:
        """Move head to maximum down angle (-22°)."""
        return self.set_angle(MIN_ANGLE, speed_dps=speed_dps)

    @property
    def last_angle(self) -> float | None:
        """Last commanded angle, or None if no movement yet."""
        return self._last_angle
