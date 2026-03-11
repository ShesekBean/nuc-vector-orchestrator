"""Bridge between Vector SDK events and the NUC event bus.

Subscribes to relevant Vector SDK events and re-emits them on the NUC bus
where downstream NUC components need to react (e.g., cliff detection →
emergency stop).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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


class SdkEventBridge:
    """Bridges Vector SDK events into the NUC event bus.

    Usage::

        bridge = SdkEventBridge(robot, nuc_bus)
        bridge.setup()
        # ... later ...
        bridge.teardown()
    """

    def __init__(self, robot: Any, nuc_bus: NucEventBus) -> None:
        self._robot = robot
        self._bus = nuc_bus
        self._subscribed = False

    def setup(self) -> None:
        """Subscribe to SDK events and wire them to the NUC bus."""
        if self._subscribed:
            return

        try:
            from anki_vector.events import Events
        except ImportError:
            logger.warning(
                "anki_vector not installed — SDK event bridge disabled"
            )
            return

        self._robot.events.subscribe(
            self._on_robot_state, Events.robot_state
        )
        self._robot.events.subscribe(
            self._on_connection_lost, Events.connection_lost
        )
        self._subscribed = True
        logger.info("SDK event bridge active")

    def teardown(self) -> None:
        """Unsubscribe from all SDK events."""
        if not self._subscribed:
            return

        try:
            from anki_vector.events import Events

            self._robot.events.unsubscribe(
                self._on_robot_state, Events.robot_state
            )
            self._robot.events.unsubscribe(
                self._on_connection_lost, Events.connection_lost
            )
        except Exception:
            logger.exception("Error during SDK event teardown")

        self._subscribed = False
        logger.info("SDK event bridge torn down")

    # -- SDK event handlers --------------------------------------------------

    def _on_robot_state(self, _robot: Any, _name: str, msg: Any) -> None:
        """Handle robot_state events — bridge cliff and touch to NUC bus."""
        # Cliff detection → emergency_stop + cliff_triggered
        cliff = getattr(msg, "cliff_detected_flags", 0)
        if cliff:
            self._bus.emit(
                CLIFF_TRIGGERED,
                CliffTriggeredEvent(cliff_flags=cliff),
            )
            self._bus.emit(
                EMERGENCY_STOP,
                EmergencyStopEvent(
                    source="cliff",
                    details=f"cliff_flags={cliff}",
                ),
            )

        # Touch detection → touch_detected
        touch = getattr(msg, "touch_detected", False)
        if touch:
            self._bus.emit(
                TOUCH_DETECTED,
                TouchDetectedEvent(location="head", is_pressed=True),
            )

    def _on_connection_lost(self, _robot: Any, _name: str, _msg: Any) -> None:
        """Handle connection_lost — emit emergency stop on NUC bus."""
        self._bus.emit(
            EMERGENCY_STOP,
            EmergencyStopEvent(source="connection_lost"),
        )
