"""Centralized behavior control manager — singleton for all services.

Solves the problem of multiple services (explorer, follow, nav, charger)
independently requesting/releasing SDK behavior control and stepping on
each other. All services go through this manager instead of calling
robot.conn.request_control() directly.

Architecture:
- Only one service can hold control at a time
- Higher priority can preempt lower priority
- When released, control drops (Vector returns to default behavior)
- Thread-safe for concurrent service requests

Usage::

    mgr = ControlManager(robot)
    mgr.acquire("explorer")    # requests OVERRIDE from SDK
    mgr.acquire("nav")         # preempts explorer (same priority)
    mgr.release("nav")         # releases control
    mgr.current_holder         # → None
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Priority levels — higher number = higher priority
PRIORITY_DEFAULT = 0
PRIORITY_OVERRIDE = 10


class ControlManager:
    """Singleton behavior control manager for Vector SDK.

    Args:
        robot: Connected anki_vector.Robot instance.
    """

    def __init__(self, robot: Any) -> None:
        self._robot = robot
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._holder_priority: int = -1
        self._has_sdk_control: bool = False

    @property
    def current_holder(self) -> str | None:
        """Name of the service currently holding control, or None."""
        with self._lock:
            return self._holder

    @property
    def has_control(self) -> bool:
        """Whether any service currently holds control."""
        with self._lock:
            return self._has_sdk_control

    def acquire(
        self, requester: str, priority: int = PRIORITY_OVERRIDE
    ) -> bool:
        """Acquire behavior control for a service.

        If another service holds control at equal or lower priority,
        it gets preempted. If higher priority holds it, acquisition fails.

        Args:
            requester: Service name (e.g., "explorer", "nav", "follow").
            priority: Priority level (default OVERRIDE).

        Returns:
            True if control was acquired, False if denied.
        """
        with self._lock:
            if self._holder == requester:
                logger.debug("Control already held by %s", requester)
                return True

            if self._holder is not None and self._holder_priority > priority:
                logger.info(
                    "Control denied for %s — held by %s at higher priority",
                    requester, self._holder,
                )
                return False

            prev = self._holder
            if prev:
                logger.info("Control preempted from %s by %s", prev, requester)

            # Request from SDK
            if not self._has_sdk_control:
                try:
                    from anki_vector.connection import ControlPriorityLevel
                    self._robot.conn.request_control(
                        behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
                    )
                    self._has_sdk_control = True
                except Exception:
                    logger.warning("SDK control request failed for %s", requester, exc_info=True)
                    return False

            self._holder = requester
            self._holder_priority = priority
            logger.info("Control acquired by %s (priority=%d)", requester, priority)
            return True

    def release(self, requester: str) -> None:
        """Release behavior control.

        Only the current holder can release. Other callers are ignored.

        Args:
            requester: Service name that wants to release.
        """
        with self._lock:
            if self._holder != requester:
                logger.debug(
                    "Release ignored — %s is not holder (holder=%s)",
                    requester, self._holder,
                )
                return

            self._holder = None
            self._holder_priority = -1

            # Release SDK control
            if self._has_sdk_control:
                try:
                    self._robot.conn.release_control()
                    self._has_sdk_control = False
                except Exception:
                    logger.warning("SDK control release failed", exc_info=True)
                    self._has_sdk_control = False

            logger.info("Control released by %s", requester)

    def force_release(self) -> None:
        """Force release control regardless of holder. Use for cleanup."""
        with self._lock:
            prev = self._holder
            self._holder = None
            self._holder_priority = -1
            if self._has_sdk_control:
                try:
                    self._robot.conn.release_control()
                except Exception:
                    pass
                self._has_sdk_control = False
            if prev:
                logger.info("Control force-released from %s", prev)
