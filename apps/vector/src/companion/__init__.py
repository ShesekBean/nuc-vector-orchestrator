"""Vector companion behavior system.

Ties together presence tracking, engagement-adaptive throttling, and
OpenClaw signalling into a single start/stop lifecycle.

Usage::

    from apps.vector.src.companion import CompanionSystem

    companion = CompanionSystem(bus)
    companion.start()
    # ...
    companion.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from apps.vector.src.companion.dispatcher import CompanionDispatcher
from apps.vector.src.companion.presence_tracker import PresenceTracker

logger = logging.getLogger(__name__)

# Periodic maintenance interval (seconds)
_MAINTENANCE_INTERVAL_S = 300.0  # 5 min


class CompanionSystem:
    """Top-level lifecycle manager for the companion behavior system.

    Parameters
    ----------
    bus : NucEventBus
        The NUC event bus for presence events.
    bridge_url : str
        Bridge HTTP base URL for battery/camera queries.
    """

    def __init__(self, bus: Any, bridge_url: str = "http://127.0.0.1:8081") -> None:
        self._bus = bus
        self._tracker = PresenceTracker(bus)
        self._dispatcher = CompanionDispatcher(bus, self._tracker, bridge_url=bridge_url)
        self._maintenance_timer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        """Start presence tracking, dispatching, and maintenance timers."""
        if self._running:
            return
        self._running = True
        self._tracker.start()
        self._dispatcher.start()
        self._schedule_maintenance()
        logger.info("CompanionSystem started")

    def stop(self) -> None:
        """Stop all companion subsystems."""
        if not self._running:
            return
        self._running = False
        self._cancel_maintenance()
        self._dispatcher.stop()
        self._tracker.stop()
        logger.info("CompanionSystem stopped")

    @property
    def tracker(self) -> PresenceTracker:
        return self._tracker

    @property
    def dispatcher(self) -> CompanionDispatcher:
        return self._dispatcher

    # -- Maintenance timer ---------------------------------------------------

    def _schedule_maintenance(self) -> None:
        self._cancel_maintenance()
        timer = threading.Timer(_MAINTENANCE_INTERVAL_S, self._on_maintenance)
        timer.daemon = True
        timer.start()
        self._maintenance_timer = timer

    def _cancel_maintenance(self) -> None:
        if self._maintenance_timer is not None:
            self._maintenance_timer.cancel()
            self._maintenance_timer = None

    def _on_maintenance(self) -> None:
        """Periodic maintenance: goodnight check, battery check."""
        if not self._running:
            return
        try:
            self._dispatcher.check_goodnight()
            self._dispatcher.check_battery()
        except Exception:
            logger.exception("Companion maintenance error")
        finally:
            if self._running:
                self._schedule_maintenance()
