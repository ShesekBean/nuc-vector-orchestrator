"""Named waypoint manager for indoor navigation.

Manages a set of named locations (e.g., "kitchen", "bedroom", "charger")
that the robot can navigate to. Waypoints are stored both in memory and
persisted to disk via MapStore.

Usage::

    mgr = WaypointManager(map_store, map_name="home")
    mgr.save("kitchen", x=1500, y=2000, theta=1.57, description="By the fridge")
    mgr.save("bedroom", x=-500, y=3000, theta=0.0)

    wp = mgr.get("kitchen")    # Waypoint(name="kitchen", x=1500, ...)
    mgr.list_waypoints()       # [Waypoint("kitchen", ...), Waypoint("bedroom", ...)]
    mgr.nearest(x=1400, y=1900)  # Waypoint("kitchen", ...)
    mgr.delete("bedroom")
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.vector.src.planner.map_store import MapStore

from apps.vector.src.planner.map_store import Waypoint

logger = logging.getLogger(__name__)


class WaypointManager:
    """Thread-safe named waypoint manager with disk persistence.

    Args:
        map_store: MapStore for persisting waypoints.
        map_name: Name of the map these waypoints belong to.
    """

    def __init__(self, map_store: MapStore, map_name: str = "default") -> None:
        self._store = map_store
        self._map_name = map_name
        self._waypoints: dict[str, Waypoint] = {}
        self._lock = threading.Lock()

        # Load existing waypoints from disk
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load waypoints from MapStore."""
        try:
            waypoints = self._store.load_waypoints(self._map_name)
            for wp in waypoints:
                self._waypoints[wp.name.lower()] = wp
            if waypoints:
                logger.info(
                    "Loaded %d waypoints from map '%s': %s",
                    len(waypoints), self._map_name,
                    ", ".join(wp.name for wp in waypoints),
                )
        except FileNotFoundError:
            logger.debug("No existing waypoints for map '%s'", self._map_name)
        except Exception:
            logger.exception("Failed to load waypoints from disk")

    def save(
        self,
        name: str,
        x: float,
        y: float,
        theta: float = 0.0,
        description: str = "",
    ) -> bool:
        """Save or update a named waypoint.

        Returns True if saved successfully.
        """
        key = name.lower().strip()
        if not key:
            logger.warning("Cannot save waypoint with empty name")
            return False

        wp = Waypoint(
            name=key,
            x=x,
            y=y,
            theta=theta,
            timestamp=time.time(),
            description=description,
        )

        with self._lock:
            is_update = key in self._waypoints
            self._waypoints[key] = wp

        action = "Updated" if is_update else "Saved"
        logger.info(
            "%s waypoint '%s' at (%.0f, %.0f) theta=%.1f deg",
            action, key, x, y, math.degrees(theta),
        )

        # Persist to disk
        self._save_to_disk()
        return True

    def get(self, name: str) -> Waypoint | None:
        """Get a waypoint by name (case-insensitive)."""
        key = name.lower().strip()
        with self._lock:
            return self._waypoints.get(key)

    def delete(self, name: str) -> bool:
        """Delete a waypoint by name.

        Returns True if deleted, False if not found.
        """
        key = name.lower().strip()
        with self._lock:
            if key not in self._waypoints:
                return False
            del self._waypoints[key]

        logger.info("Deleted waypoint '%s'", key)
        self._save_to_disk()
        return True

    def list_waypoints(self) -> list[Waypoint]:
        """Return all waypoints sorted by name."""
        with self._lock:
            return sorted(self._waypoints.values(), key=lambda wp: wp.name)

    def nearest(self, x: float, y: float) -> Waypoint | None:
        """Find the nearest waypoint to a given position.

        Returns None if no waypoints exist.
        """
        with self._lock:
            if not self._waypoints:
                return None
            return min(
                self._waypoints.values(),
                key=lambda wp: math.hypot(wp.x - x, wp.y - y),
            )

    def distance_to(self, name: str, x: float, y: float) -> float | None:
        """Compute distance from position to a named waypoint.

        Returns None if waypoint not found.
        """
        wp = self.get(name)
        if wp is None:
            return None
        return math.hypot(wp.x - x, wp.y - y)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._waypoints)

    def _save_to_disk(self) -> None:
        """Persist current waypoints to MapStore."""
        try:
            waypoints = self.list_waypoints()
            self._store.save_waypoints(self._map_name, waypoints)
        except FileNotFoundError:
            # Map doesn't exist yet on disk — will be saved when NavController saves full map
            logger.debug("Map '%s' doesn't exist on disk yet, waypoints will be saved with map", self._map_name)
        except Exception:
            logger.exception("Failed to persist waypoints to disk")
