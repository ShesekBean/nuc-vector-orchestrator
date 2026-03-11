"""Lift motor controller for Vector's motorized forklift.

Wraps the Vector SDK ``set_lift_height`` API with named presets, range
clamping, speed control, and auto-stow on idle.  Integrates with the
NUC event bus for emergency-stop handling and lift-state notifications.

Usage::

    from apps.vector.src.lift_controller import LiftController

    ctrl = LiftController(robot, nuc_bus)
    ctrl.start()
    ctrl.move_to(0.6)              # absolute height (0.0–1.0)
    ctrl.move_to_preset("carry")   # named preset
    ctrl.stow()                    # return to down position
    ctrl.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    LIFT_HEIGHT_CHANGED,
    LiftHeightChangedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# --- Presets ----------------------------------------------------------------

PRESETS: dict[str, float] = {
    "stowed": 0.0,
    "carry": 0.5,
    "high": 0.8,
}

# --- Defaults ---------------------------------------------------------------

DEFAULT_AUTO_STOW_TIMEOUT_S = 30.0
MIN_HEIGHT = 0.0
MAX_HEIGHT = 1.0


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


class LiftController:
    """Manages Vector's lift motor with presets and auto-stow.

    Parameters
    ----------
    robot:
        Connected ``anki_vector.Robot`` instance.
    nuc_bus:
        NUC event bus for emergency-stop subscription and lift notifications.
    auto_stow_timeout_s:
        Seconds of inactivity before the lift auto-stows.  Set to ``0`` or
        negative to disable auto-stow.
    """

    def __init__(
        self,
        robot: Any,
        nuc_bus: NucEventBus,
        auto_stow_timeout_s: float = DEFAULT_AUTO_STOW_TIMEOUT_S,
    ) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._auto_stow_timeout_s = auto_stow_timeout_s

        self._lock = threading.Lock()
        self._target_height: float = 0.0
        self._running = False
        self._e_stop_active = False

        # Auto-stow timer
        self._stow_timer: threading.Timer | None = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to events and enable auto-stow."""
        if self._running:
            return
        self._bus.on(EMERGENCY_STOP, self._on_emergency_stop)
        self._running = True
        self._e_stop_active = False
        logger.info("LiftController started")

    def stop(self) -> None:
        """Unsubscribe from events and cancel pending auto-stow."""
        if not self._running:
            return
        self._cancel_stow_timer()
        self._bus.off(EMERGENCY_STOP, self._on_emergency_stop)
        self._running = False
        logger.info("LiftController stopped")

    # -- Public API ----------------------------------------------------------

    def move_to(self, height: float, speed: float | None = None) -> bool:
        """Move lift to an absolute height.

        Parameters
        ----------
        height:
            Target height in the normalised range ``[0.0, 1.0]``.
            Values outside this range are clamped.
        speed:
            Optional ``max_speed`` passed to the SDK.  ``None`` uses the
            SDK default.

        Returns
        -------
        bool
            ``True`` if the command was sent, ``False`` if blocked (e.g.
            emergency-stop active or controller not running).
        """
        if not self._running:
            logger.warning("move_to called but LiftController is not running")
            return False

        with self._lock:
            if self._e_stop_active:
                logger.warning("Lift move blocked — emergency stop active")
                return False

        clamped = _clamp(height, MIN_HEIGHT, MAX_HEIGHT)
        if clamped != height:
            logger.debug("Lift height clamped: %.3f → %.3f", height, clamped)

        try:
            kwargs: dict[str, float] = {}
            if speed is not None:
                kwargs["max_speed"] = speed
            self._robot.behavior.set_lift_height(clamped, **kwargs)
        except Exception:
            logger.exception("Failed to set lift height to %.2f", clamped)
            return False

        with self._lock:
            self._target_height = clamped

        self._reset_stow_timer()
        self._bus.emit(
            LIFT_HEIGHT_CHANGED,
            LiftHeightChangedEvent(height=clamped, preset=None),
        )
        logger.info("Lift moved to %.2f", clamped)
        return True

    def move_to_preset(self, name: str, speed: float | None = None) -> bool:
        """Move lift to a named preset position.

        Parameters
        ----------
        name:
            One of ``"stowed"``, ``"carry"``, ``"high"``.
        speed:
            Optional max speed.

        Returns
        -------
        bool
            ``True`` if command sent, ``False`` otherwise.

        Raises
        ------
        ValueError
            If *name* is not a recognised preset.
        """
        key = name.lower()
        if key not in PRESETS:
            raise ValueError(
                f"Unknown preset {name!r}; choose from {sorted(PRESETS)}"
            )
        height = PRESETS[key]
        success = self.move_to(height, speed=speed)
        if success:
            # Re-emit with preset name for richer event data.
            self._bus.emit(
                LIFT_HEIGHT_CHANGED,
                LiftHeightChangedEvent(height=height, preset=key),
            )
        return success

    def stow(self, speed: float | None = None) -> bool:
        """Convenience method — move lift to the stowed (down) position."""
        return self.move_to_preset("stowed", speed=speed)

    # -- Properties ----------------------------------------------------------

    @property
    def current_target(self) -> float:
        """Last commanded target height (0.0–1.0)."""
        with self._lock:
            return self._target_height

    @property
    def is_stowed(self) -> bool:
        """Whether the lift target is at the stowed position."""
        with self._lock:
            return self._target_height == PRESETS["stowed"]

    # -- Event handlers ------------------------------------------------------

    def _on_emergency_stop(self, event: Any) -> None:
        """Block further lift commands during an emergency stop."""
        with self._lock:
            self._e_stop_active = True
        self._cancel_stow_timer()
        logger.warning("LiftController: emergency stop — lift commands blocked")

    def clear_emergency_stop(self) -> None:
        """Re-enable lift commands after the emergency-stop condition clears."""
        with self._lock:
            self._e_stop_active = False
        logger.info("LiftController: emergency stop cleared")

    # -- Auto-stow timer -----------------------------------------------------

    def _reset_stow_timer(self) -> None:
        """(Re)start the auto-stow countdown."""
        self._cancel_stow_timer()
        if self._auto_stow_timeout_s <= 0:
            return
        # Don't schedule if already stowed.
        with self._lock:
            if self._target_height == PRESETS["stowed"]:
                return
        self._stow_timer = threading.Timer(
            self._auto_stow_timeout_s, self._auto_stow,
        )
        self._stow_timer.daemon = True
        self._stow_timer.start()

    def _cancel_stow_timer(self) -> None:
        """Cancel the pending auto-stow timer, if any."""
        if self._stow_timer is not None:
            self._stow_timer.cancel()
            self._stow_timer = None

    def _auto_stow(self) -> None:
        """Timer callback — stow the lift after idle timeout."""
        logger.info("Auto-stow: returning lift to stowed position")
        self.stow()
