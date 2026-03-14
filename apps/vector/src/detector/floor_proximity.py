"""Floor-line proximity detector — fast wall/obstacle detection using edge analysis.

Vector's camera is at a fixed height (~30mm) and angle. When an obstacle is
close, the floor-obstacle boundary line rises in the frame. By detecting the
highest strong horizontal edge in the lower half of the frame, we estimate
proximity to obstacles in three columns (left, center, right).

Runs in ~5ms per frame — fast enough for 200Hz control loops.

Usage::

    detector = FloorProximityDetector()
    reading = detector.detect(bgr_frame)
    if reading.center_mm < 100:
        print("Wall ahead!")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Calibration: pixel row in lower half → approximate distance in mm.
# Vector camera is ~30mm high, ~10° down tilt, 120° FOV, 800x600.
# Row 0 = middle of frame (far), Row 299 = bottom of frame (closest).
# These are approximate — should be refined with calibration script.
# Format: (row_from_bottom, distance_mm)
_ROW_TO_DIST = [
    (10, 30),    # very bottom of frame = ~30mm ahead
    (30, 50),
    (60, 80),
    (90, 120),
    (120, 180),
    (150, 250),
    (180, 350),
    (210, 500),
    (240, 700),
    (270, 1000),
    (300, 1500),  # middle of frame = ~1500mm ahead
]


@dataclass
class ProximityReading:
    """Proximity estimate in three forward columns."""

    left_mm: float = 1500.0    # distance to obstacle in left third
    center_mm: float = 1500.0  # distance to obstacle in center third
    right_mm: float = 1500.0   # distance to obstacle in right third
    confidence: float = 0.0    # 0-1, based on edge strength
    min_mm: float = 1500.0     # minimum of all three

    @property
    def is_blocked(self) -> bool:
        """True if any column has an obstacle within danger distance."""
        return self.min_mm < 100

    @property
    def is_caution(self) -> bool:
        """True if any column has an obstacle within caution distance."""
        return self.min_mm < 250

    @property
    def suggested_turn(self) -> str:
        """Which direction has more clearance: 'left', 'right', or ''."""
        if not self.is_blocked and not self.is_caution:
            return ""
        if self.left_mm > self.right_mm:
            return "left"
        return "right"


def _row_to_distance(row_from_bottom: int) -> float:
    """Interpolate pixel row to distance in mm."""
    if row_from_bottom <= _ROW_TO_DIST[0][0]:
        return _ROW_TO_DIST[0][1]
    if row_from_bottom >= _ROW_TO_DIST[-1][0]:
        return _ROW_TO_DIST[-1][1]

    for i in range(len(_ROW_TO_DIST) - 1):
        r0, d0 = _ROW_TO_DIST[i]
        r1, d1 = _ROW_TO_DIST[i + 1]
        if r0 <= row_from_bottom <= r1:
            t = (row_from_bottom - r0) / (r1 - r0)
            return d0 + t * (d1 - d0)

    return 1500.0


class FloorProximityDetector:
    """Detects obstacle proximity using floor-line edge analysis.

    Analyzes the lower half of the camera frame for strong horizontal
    edges that indicate a floor-to-obstacle boundary. The higher the
    edge appears in the frame, the closer the obstacle.

    Args:
        canny_low: Canny edge detection low threshold (tuned for Vector's dark camera).
        canny_high: Canny edge detection high threshold.
        min_edge_strength: Minimum number of edge pixels in a row to count as a boundary.
    """

    def __init__(
        self,
        canny_low: int = 25,
        canny_high: int = 75,
        min_edge_strength: int = 15,
    ) -> None:
        self._canny_low = canny_low
        self._canny_high = canny_high
        self._min_edge_strength = min_edge_strength

    def detect(self, frame: np.ndarray) -> ProximityReading:
        """Analyze a BGR frame for obstacle proximity.

        Args:
            frame: BGR numpy array (800x600 from Vector camera).

        Returns:
            ProximityReading with distance estimates for left/center/right.
        """
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        half_h = h // 2

        # Only analyze lower half of frame (where floor/obstacles appear)
        lower = frame[half_h:, :]

        # Preprocess: histogram equalization on grayscale for Vector's dark camera
        gray = cv2.cvtColor(lower, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)

        # Light blur to reduce noise
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        # Canny edge detection
        edges = cv2.Canny(gray, self._canny_low, self._canny_high)

        # Split into three vertical columns
        col_w = w // 3
        columns = [
            edges[:, :col_w],           # left
            edges[:, col_w:2*col_w],    # center
            edges[:, 2*col_w:],         # right
        ]

        distances = []
        total_confidence = 0.0

        for col_edges in columns:
            col_h = col_edges.shape[0]
            best_row = -1

            # Scan from top of lower half downward (far to near)
            # Find the highest row with enough edge pixels
            for row in range(col_h):
                edge_count = int(np.sum(col_edges[row, :] > 0))
                if edge_count >= self._min_edge_strength:
                    best_row = row
                    break

            if best_row >= 0:
                # Convert row position to distance
                row_from_bottom = col_h - best_row
                dist = _row_to_distance(row_from_bottom)
                distances.append(dist)
                # Confidence based on how strong the edge was
                edge_strength = int(np.sum(col_edges[best_row, :] > 0))
                total_confidence += min(1.0, edge_strength / (col_w * 0.3))
            else:
                distances.append(1500.0)  # no edge = far away

        avg_confidence = total_confidence / 3.0

        reading = ProximityReading(
            left_mm=distances[0],
            center_mm=distances[1],
            right_mm=distances[2],
            confidence=round(avg_confidence, 2),
            min_mm=min(distances),
        )

        return reading
