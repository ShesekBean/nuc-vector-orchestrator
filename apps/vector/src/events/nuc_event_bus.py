"""Thread-safe in-process pub/sub event bus for NUC-side computed events.

Lightweight (~100 lines), no external dependencies. Designed for events that
don't exist in the Vector SDK — YOLO detections, face recognition, follow
state, motor commands, STT results, etc.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[Any], None]


class NucEventBus:
    """Thread-safe publish/subscribe event bus.

    Usage::

        bus = NucEventBus()
        bus.on("yolo_person_detected", my_handler)
        bus.emit("yolo_person_detected", detection_data)
        bus.off("yolo_person_detected", my_handler)
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callback]] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, event: str, callback: Callback) -> None:
        """Subscribe *callback* to *event*."""
        with self._lock:
            if callback not in self._listeners[event]:
                self._listeners[event].append(callback)

    def off(self, event: str, callback: Callback) -> None:
        """Unsubscribe *callback* from *event*.

        Silently ignores callbacks that are not subscribed.
        """
        with self._lock:
            try:
                self._listeners[event].remove(callback)
            except ValueError:
                pass

    def emit(self, event: str, data: Any = None) -> None:
        """Publish *event* with optional *data* to all subscribers.

        Each callback is invoked in the caller's thread. Exceptions in
        individual callbacks are logged but do not prevent other subscribers
        from being notified.
        """
        with self._lock:
            listeners = list(self._listeners[event])
        for cb in listeners:
            try:
                cb(data)
            except Exception:
                logger.exception(
                    "Error in event callback %s for event %r", cb, event
                )

    def once(self, event: str, callback: Callback) -> None:
        """Subscribe *callback* to fire only once for *event*."""

        def _wrapper(data: Any) -> None:
            self.off(event, _wrapper)
            callback(data)

        self.on(event, _wrapper)

    def clear(self, event: str | None = None) -> None:
        """Remove all listeners for *event*, or all events if *event* is None."""
        with self._lock:
            if event is None:
                self._listeners.clear()
            else:
                self._listeners.pop(event, None)

    def listener_count(self, event: str) -> int:
        """Return the number of listeners for *event*."""
        with self._lock:
            return len(self._listeners[event])
