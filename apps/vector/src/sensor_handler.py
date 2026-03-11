"""Sensor handler for Vector cliff detection and touch events.

Bridges Vector SDK robot_state events to the NUC event bus and executes
safety-critical actions (motor stop on cliff detection). Touch events are
debounced and forwarded for interaction handling.

Safety-critical: cliff response targets <50ms latency by executing motor
stop synchronously in the event callback — no queuing or async overhead.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    EMERGENCY_STOP,
    TOUCH_DETECTED,
    CliffTriggeredEvent,
    EmergencyStopEvent,
    TouchDetectedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Debounce interval for touch events (seconds).
# Vector's capacitive sensor can fire rapidly — collapse bursts.
TOUCH_DEBOUNCE_S = 0.3


class SensorHandler:
    """Manages cliff and touch sensor events from Vector.

    Subscribes to NUC bus events emitted by SdkEventBridge and takes
    safety-critical actions (motor stop) or forwards to interaction
    callbacks.

    Usage::

        handler = SensorHandler(robot, nuc_bus)
        handler.start()
        # ... robot is running ...
        handler.stop()
    """

    def __init__(
        self,
        robot: Any,
        nuc_bus: NucEventBus,
        touch_debounce_s: float = TOUCH_DEBOUNCE_S,
    ) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._touch_debounce_s = touch_debounce_s
        self._last_touch_time: float = 0.0
        self._running = False
        self._touch_callbacks: list[Callable[[TouchDetectedEvent], None]] = []
        self._cliff_count = 0

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to NUC bus events for cliff and touch handling."""
        if self._running:
            return
        self._bus.on(EMERGENCY_STOP, self._on_emergency_stop)
        self._bus.on(CLIFF_TRIGGERED, self._on_cliff_triggered)
        self._bus.on(TOUCH_DETECTED, self._on_touch_detected)
        self._running = True
        self._cliff_count = 0
        logger.info("SensorHandler started")

    def stop(self) -> None:
        """Unsubscribe from all NUC bus events."""
        if not self._running:
            return
        self._bus.off(EMERGENCY_STOP, self._on_emergency_stop)
        self._bus.off(CLIFF_TRIGGERED, self._on_cliff_triggered)
        self._bus.off(TOUCH_DETECTED, self._on_touch_detected)
        self._running = False
        logger.info("SensorHandler stopped (cliff_count=%d)", self._cliff_count)

    @property
    def cliff_count(self) -> int:
        """Number of cliff events handled since start."""
        return self._cliff_count

    # -- Touch callback registration -----------------------------------------

    def on_touch(self, callback: Callable[[TouchDetectedEvent], None]) -> None:
        """Register a callback for debounced touch events."""
        if callback not in self._touch_callbacks:
            self._touch_callbacks.append(callback)

    def off_touch(self, callback: Callable[[TouchDetectedEvent], None]) -> None:
        """Unregister a touch callback."""
        try:
            self._touch_callbacks.remove(callback)
        except ValueError:
            pass

    # -- Event handlers ------------------------------------------------------

    def _on_emergency_stop(self, event: EmergencyStopEvent) -> None:
        """Execute immediate motor stop on emergency_stop events.

        This is the safety-critical path — must complete in <50ms.
        Called synchronously in the emitter's thread to minimize latency.
        """
        self._stop_motors()

    def _on_cliff_triggered(self, event: CliffTriggeredEvent) -> None:
        """Track cliff events for diagnostics."""
        self._cliff_count += 1
        logger.warning(
            "Cliff detected (flags=%d, count=%d)",
            event.cliff_flags,
            self._cliff_count,
        )

    def _on_touch_detected(self, event: TouchDetectedEvent) -> None:
        """Debounce touch events and forward to registered callbacks."""
        now = time.monotonic()
        if now - self._last_touch_time < self._touch_debounce_s:
            return
        self._last_touch_time = now
        logger.debug("Touch detected (location=%s)", event.location)
        for cb in list(self._touch_callbacks):
            try:
                cb(event)
            except Exception:
                logger.exception("Error in touch callback %s", cb)

    # -- Motor control -------------------------------------------------------

    def _stop_motors(self) -> None:
        """Send immediate motor stop command to Vector.

        Uses set_wheel_motors with zero speeds for fastest response.
        """
        try:
            self._robot.motors.set_wheel_motors(0.0, 0.0, 0.0, 0.0)
            logger.info("Motors stopped (emergency)")
        except Exception:
            logger.exception("Failed to stop motors")
