"""Persistent map storage for Vector's indoor navigation.

Saves and loads occupancy grids, visual landmarks, and named waypoints
to disk so the robot can resume navigation across restarts.

Storage format: JSON metadata + numpy binary for grid data.
Maps are stored in ~/.vector/maps/ with a name-based directory structure::

    ~/.vector/maps/
    +-- home/
    |   +-- metadata.json   (grid params, waypoints, stats)
    |   +-- grid.npy        (occupancy grid as numpy array)
    |   +-- landmarks.npy   (ORB descriptors for loop closure)
    +-- office/
        +-- ...

Usage::

    store = MapStore()
    store.save("home", slam.get_grid(), slam._landmarks, waypoints)
    grid, landmarks, waypoints = store.load("home")
    store.list_maps()  # ["home", "office"]
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAP_DIR = os.path.expanduser("~/.vector/maps")


@dataclass
class Waypoint:
    """A named location in the map."""

    name: str
    x: float  # mm
    y: float  # mm
    theta: float  # heading in radians (direction robot was facing when saved)
    timestamp: float = 0.0  # epoch when waypoint was saved
    description: str = ""  # optional human-readable description

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Waypoint:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class MapMetadata:
    """Metadata about a saved map."""

    name: str
    grid_size_mm: int
    cell_size_mm: int
    grid_dim: int
    waypoint_count: int
    landmark_count: int
    free_cells: int
    occupied_cells: int
    created_at: float  # epoch
    updated_at: float  # epoch
    total_frames_processed: int = 0
    loop_closures: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MapMetadata:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class MapStore:
    """Persistent storage for navigation maps.

    Thread-safe: save/load operations acquire a lock to prevent
    concurrent writes to the same map.

    Args:
        map_dir: Directory to store maps. Defaults to ~/.vector/maps/
    """

    def __init__(self, map_dir: str = DEFAULT_MAP_DIR) -> None:
        self._map_dir = Path(map_dir)
        self._map_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        name: str,
        grid: Any,  # OccupancyGrid
        landmarks: list[Any] | None = None,  # list[VisualLandmark]
        waypoints: list[Waypoint] | None = None,
        total_frames: int = 0,
        loop_closures: int = 0,
    ) -> Path:
        """Save a map to disk.

        Args:
            name: Map name (used as directory name).
            grid: OccupancyGrid instance.
            landmarks: Visual landmarks for loop closure.
            waypoints: Named waypoints.
            total_frames: Total frames processed during mapping.
            loop_closures: Number of loop closures detected.

        Returns:
            Path to the saved map directory.
        """
        import numpy as np

        map_path = self._map_dir / _sanitize_name(name)
        map_path.mkdir(parents=True, exist_ok=True)

        now = time.time()

        # Load existing metadata to preserve created_at
        meta_path = map_path / "metadata.json"
        created_at = now
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    old_meta = json.load(f)
                created_at = old_meta.get("created_at", now)
            except Exception:
                pass

        # Save occupancy grid
        grid_path = map_path / "grid.npy"
        np.save(str(grid_path), grid.grid)

        # Save landmarks
        landmark_count = 0
        if landmarks:
            landmark_data = []
            for lm in landmarks:
                landmark_data.append({
                    "x": lm.x,
                    "y": lm.y,
                    "frame_id": lm.frame_id,
                })
            landmarks_path = map_path / "landmarks.json"
            with open(landmarks_path, "w") as f:
                json.dump(landmark_data, f)

            # Save landmark descriptors as numpy array
            descriptors = [lm.descriptors for lm in landmarks if lm.descriptors is not None]
            if descriptors:
                desc_array = np.array(descriptors)
                np.save(str(map_path / "landmark_descriptors.npy"), desc_array)
            landmark_count = len(landmarks)

        # Save waypoints
        wp_list = []
        if waypoints:
            wp_list = [wp.to_dict() for wp in waypoints]
        wp_path = map_path / "waypoints.json"
        with open(wp_path, "w") as f:
            json.dump(wp_list, f, indent=2)

        # Save metadata
        metadata = MapMetadata(
            name=name,
            grid_size_mm=grid.size_mm,
            cell_size_mm=grid.cell_size_mm,
            grid_dim=grid.grid_dim,
            waypoint_count=len(wp_list),
            landmark_count=landmark_count,
            free_cells=grid.free_cell_count,
            occupied_cells=grid.occupied_cell_count,
            created_at=created_at,
            updated_at=now,
            total_frames_processed=total_frames,
            loop_closures=loop_closures,
        )
        with open(meta_path, "w") as f:
            json.dump(metadata.to_dict(), f, indent=2)

        logger.info(
            "Map '%s' saved: %d free, %d occupied, %d waypoints, %d landmarks",
            name, grid.free_cell_count, grid.occupied_cell_count,
            len(wp_list), landmark_count,
        )
        return map_path

    def load(self, name: str) -> tuple[Any, list[Any], list[Waypoint], MapMetadata]:
        """Load a map from disk.

        Returns:
            (grid_array, landmarks, waypoints, metadata)
            grid_array is a numpy ndarray (caller wraps in OccupancyGrid).
            landmarks is a list of dicts (caller reconstructs VisualLandmark).
        """
        import numpy as np

        map_path = self._map_dir / _sanitize_name(name)
        if not map_path.exists():
            raise FileNotFoundError(f"Map '{name}' not found at {map_path}")

        # Load metadata
        meta_path = map_path / "metadata.json"
        with open(meta_path) as f:
            meta_dict = json.load(f)
        metadata = MapMetadata.from_dict(meta_dict)

        # Load grid
        grid_path = map_path / "grid.npy"
        grid_array = np.load(str(grid_path))

        # Load landmarks
        landmarks = []
        landmarks_path = map_path / "landmarks.json"
        if landmarks_path.exists():
            with open(landmarks_path) as f:
                landmarks = json.load(f)

            # Load descriptors if available
            desc_path = map_path / "landmark_descriptors.npy"
            if desc_path.exists():
                desc_array = np.load(str(desc_path))
                for i, lm in enumerate(landmarks):
                    if i < len(desc_array):
                        lm["descriptors"] = desc_array[i]

        # Load waypoints
        waypoints = []
        wp_path = map_path / "waypoints.json"
        if wp_path.exists():
            with open(wp_path) as f:
                wp_list = json.load(f)
            waypoints = [Waypoint.from_dict(wp) for wp in wp_list]

        logger.info(
            "Map '%s' loaded: grid=%dx%d, %d waypoints, %d landmarks",
            name, metadata.grid_dim, metadata.grid_dim,
            len(waypoints), len(landmarks),
        )
        return grid_array, landmarks, waypoints, metadata

    def load_waypoints(self, name: str) -> list[Waypoint]:
        """Load only waypoints from a map (lightweight)."""
        map_path = self._map_dir / _sanitize_name(name)
        wp_path = map_path / "waypoints.json"
        if not wp_path.exists():
            return []
        with open(wp_path) as f:
            wp_list = json.load(f)
        return [Waypoint.from_dict(wp) for wp in wp_list]

    def save_waypoints(self, name: str, waypoints: list[Waypoint]) -> None:
        """Save waypoints to an existing map (lightweight update)."""
        map_path = self._map_dir / _sanitize_name(name)
        if not map_path.exists():
            raise FileNotFoundError(f"Map '{name}' not found")

        wp_path = map_path / "waypoints.json"
        with open(wp_path, "w") as f:
            json.dump([wp.to_dict() for wp in waypoints], f, indent=2)

        # Update metadata waypoint count
        meta_path = map_path / "metadata.json"
        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
            meta["waypoint_count"] = len(waypoints)
            meta["updated_at"] = time.time()
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)

        logger.info("Saved %d waypoints to map '%s'", len(waypoints), name)

    def list_maps(self) -> list[dict]:
        """List all saved maps with their metadata summary."""
        maps = []
        for entry in sorted(self._map_dir.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                maps.append({
                    "name": meta.get("name", entry.name),
                    "waypoint_count": meta.get("waypoint_count", 0),
                    "free_cells": meta.get("free_cells", 0),
                    "occupied_cells": meta.get("occupied_cells", 0),
                    "updated_at": meta.get("updated_at", 0),
                })
            except Exception:
                logger.warning("Failed to read metadata for %s", entry.name)
        return maps

    def delete_map(self, name: str) -> bool:
        """Delete a saved map."""
        import shutil

        map_path = self._map_dir / _sanitize_name(name)
        if not map_path.exists():
            return False
        shutil.rmtree(map_path)
        logger.info("Deleted map '%s'", name)
        return True

    def exists(self, name: str) -> bool:
        """Check if a map exists."""
        map_path = self._map_dir / _sanitize_name(name)
        return (map_path / "metadata.json").exists()


def _sanitize_name(name: str) -> str:
    """Sanitize map name for use as directory name."""
    # Replace spaces with hyphens, remove non-alphanumeric except hyphen/underscore
    sanitized = name.lower().strip()
    sanitized = sanitized.replace(" ", "-")
    sanitized = "".join(c for c in sanitized if c.isalnum() or c in "-_")
    return sanitized or "default"
