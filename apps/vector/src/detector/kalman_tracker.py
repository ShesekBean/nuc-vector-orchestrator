"""Kalman filter tracker for smoothing YOLO person detections.

Position-only Kalman prediction (cx, cy) with frozen bbox dimensions (w, h).
R3 lesson: predicting bbox size caused wild oscillation because YOLO sizes
vary naturally, and the planner uses bbox_h for distance estimation.

Architecture::

    YOLO (~5-15fps OpenVINO) ──► KalmanTracker.update(detections)
                                       │
    KalmanTracker.predict() (10Hz) ◄───┘
             │
             ▼
        TrackedPersonEvent (cx, cy, frozen_w, frozen_h)
             │
             ├──► Head tracker
             └──► Follow planner
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class KalmanTrack:
    """Single-target Kalman filter tracking center position only.

    State vector: [cx, cy, vx, vy]
    Measurement:  [cx, cy]

    Bbox dimensions (w, h) are frozen at last-measured values — NOT predicted.
    """

    track_id: int
    # Frozen bbox dimensions (updated only on measurement, never predicted)
    width: float
    height: float
    confidence: float
    # Track lifecycle
    age: int = 0  # frames since creation
    hits: int = 0  # number of YOLO measurements received
    time_since_update: int = 0  # frames since last measurement

    # Kalman state — initialized in __post_init__
    _x: np.ndarray = field(init=False, repr=False)  # state [cx, cy, vx, vy]
    _P: np.ndarray = field(init=False, repr=False)  # covariance
    _F: np.ndarray = field(init=False, repr=False)  # transition matrix
    _H: np.ndarray = field(init=False, repr=False)  # measurement matrix
    _Q: np.ndarray = field(init=False, repr=False)  # process noise
    _R: np.ndarray = field(init=False, repr=False)  # measurement noise

    def __post_init__(self) -> None:
        # State: [cx, cy, vx, vy]
        self._x = np.zeros(4, dtype=np.float64)
        # Covariance — high initial uncertainty for velocity
        self._P = np.diag([10.0, 10.0, 100.0, 100.0])
        # State transition (constant velocity, dt=1 initially, updated in predict)
        self._F = np.eye(4, dtype=np.float64)
        # Measurement matrix — observe cx, cy only
        self._H = np.zeros((2, 4), dtype=np.float64)
        self._H[0, 0] = 1.0
        self._H[1, 1] = 1.0
        # Process noise (tuned for pixel-space at ~10Hz)
        self._Q = np.diag([1.0, 1.0, 5.0, 5.0])
        # Measurement noise (YOLO detection jitter ~5-10px)
        self._R = np.diag([8.0, 8.0])

    def init_state(self, cx: float, cy: float) -> None:
        """Set initial position (velocity starts at zero)."""
        self._x[0] = cx
        self._x[1] = cy
        self._x[2] = 0.0
        self._x[3] = 0.0
        self.hits = 1

    def predict(self, dt: float = 0.1) -> tuple[float, float]:
        """Predict next state. Returns predicted (cx, cy).

        Args:
            dt: time step in seconds (default 0.1 = 10Hz).
        """
        # Update transition matrix with actual dt
        self._F[0, 2] = dt
        self._F[1, 3] = dt

        self._x = self._F @ self._x
        self._P = self._F @ self._P @ self._F.T + self._Q * dt

        self.age += 1
        self.time_since_update += 1

        return float(self._x[0]), float(self._x[1])

    def update(self, cx: float, cy: float, w: float, h: float,
               confidence: float) -> None:
        """Incorporate a YOLO measurement.

        Updates position state via Kalman equations.
        Freezes bbox dimensions at measured values (no prediction).
        """
        z = np.array([cx, cy], dtype=np.float64)

        # Innovation
        y = z - self._H @ self._x
        # Innovation covariance
        S = self._H @ self._P @ self._H.T + self._R
        # Kalman gain
        K = self._P @ self._H.T @ np.linalg.inv(S)
        # State update
        self._x = self._x + K @ y
        # Covariance update (Joseph form for numerical stability)
        I_KH = np.eye(4) - K @ self._H
        self._P = I_KH @ self._P @ I_KH.T + K @ self._R @ K.T

        # Freeze dimensions at measured values
        self.width = w
        self.height = h
        self.confidence = confidence
        self.hits += 1
        self.time_since_update = 0

    @property
    def cx(self) -> float:
        return float(self._x[0])

    @property
    def cy(self) -> float:
        return float(self._x[1])

    @property
    def vx(self) -> float:
        return float(self._x[2])

    @property
    def vy(self) -> float:
        return float(self._x[3])


def _iou(box_a: tuple[float, float, float, float],
         box_b: tuple[float, float, float, float]) -> float:
    """Compute IoU between two boxes (cx, cy, w, h)."""
    ax1 = box_a[0] - box_a[2] / 2
    ay1 = box_a[1] - box_a[3] / 2
    ax2 = box_a[0] + box_a[2] / 2
    ay2 = box_a[1] + box_a[3] / 2

    bx1 = box_b[0] - box_b[2] / 2
    by1 = box_b[1] - box_b[3] / 2
    bx2 = box_b[0] + box_b[2] / 2
    by2 = box_b[1] + box_b[3] / 2

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = box_a[2] * box_a[3]
    area_b = box_b[2] * box_b[3]
    union = area_a + area_b - inter

    if union <= 0:
        return 0.0
    return inter / union


@dataclass
class Detection:
    """A single YOLO detection in pixel coordinates."""

    cx: float
    cy: float
    width: float
    height: float
    confidence: float


class KalmanTracker:
    """Multi-target Kalman tracker with IoU-based assignment.

    Manages track lifecycle: creation, update, prediction, and deletion.
    Emits smoothed detections at a configurable prediction rate.

    Args:
        max_age: Delete track after this many frames without a measurement.
        min_hits: Track must have this many hits before it is considered
            confirmed (emitted to consumers).
        iou_threshold: Minimum IoU to match a detection to an existing track.
        prediction_rate_hz: Target prediction rate (default 10Hz).
    """

    def __init__(
        self,
        max_age: int = 30,
        min_hits: int = 3,
        iou_threshold: float = 0.2,
        prediction_rate_hz: float = 10.0,
    ) -> None:
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.prediction_rate_hz = prediction_rate_hz

        self._tracks: list[KalmanTrack] = []
        self._next_id: int = 1
        self._lock = threading.Lock()
        self._last_predict_time: float = 0.0

    def update(self, detections: list[Detection]) -> list[KalmanTrack]:
        """Process a batch of YOLO detections (one frame).

        Matches detections to existing tracks via IoU, updates matched tracks,
        creates new tracks for unmatched detections, and removes stale tracks.

        Returns:
            List of confirmed tracks (hits >= min_hits).
        """
        with self._lock:
            return self._update_locked(detections)

    def _update_locked(self, detections: list[Detection]) -> list[KalmanTrack]:
        # Predict all existing tracks forward
        now = time.monotonic()
        if self._last_predict_time > 0:
            dt = now - self._last_predict_time
        else:
            dt = 1.0 / self.prediction_rate_hz
        self._last_predict_time = now

        for track in self._tracks:
            track.predict(dt)

        if not detections:
            return self._prune_and_return()

        if not self._tracks:
            # No existing tracks — create one per detection
            for det in detections:
                self._create_track(det)
            return self._prune_and_return()

        # Build IoU cost matrix
        n_tracks = len(self._tracks)
        n_dets = len(detections)
        iou_matrix = np.zeros((n_tracks, n_dets), dtype=np.float64)

        for t, track in enumerate(self._tracks):
            t_box = (track.cx, track.cy, track.width, track.height)
            for d, det in enumerate(detections):
                d_box = (det.cx, det.cy, det.width, det.height)
                iou_matrix[t, d] = _iou(t_box, d_box)

        # Greedy assignment (sufficient for typically 1-3 persons)
        matched_tracks: set[int] = set()
        matched_dets: set[int] = set()

        while True:
            if iou_matrix.size == 0:
                break
            best = np.unravel_index(np.argmax(iou_matrix), iou_matrix.shape)
            best_t, best_d = int(best[0]), int(best[1])
            if iou_matrix[best_t, best_d] < self.iou_threshold:
                break
            # Match found
            self._tracks[best_t].update(
                detections[best_d].cx,
                detections[best_d].cy,
                detections[best_d].width,
                detections[best_d].height,
                detections[best_d].confidence,
            )
            matched_tracks.add(best_t)
            matched_dets.add(best_d)
            # Zero out row and column to prevent re-matching
            iou_matrix[best_t, :] = 0.0
            iou_matrix[:, best_d] = 0.0

        # Create new tracks for unmatched detections
        for d, det in enumerate(detections):
            if d not in matched_dets:
                self._create_track(det)

        return self._prune_and_return()

    def predict(self) -> list[KalmanTrack]:
        """Run a prediction step (no new measurements).

        Call this at the prediction rate (e.g., 10Hz) between YOLO frames
        to get smoothed positions.

        Returns:
            List of confirmed tracks.
        """
        with self._lock:
            now = time.monotonic()
            if self._last_predict_time > 0:
                dt = now - self._last_predict_time
            else:
                dt = 1.0 / self.prediction_rate_hz
            self._last_predict_time = now

            for track in self._tracks:
                track.predict(dt)

            return self._prune_and_return()

    def get_primary_track(self) -> KalmanTrack | None:
        """Return the longest-lived confirmed track (best follow target).

        Prefers tracks with more hits (more YOLO measurements received).
        """
        with self._lock:
            confirmed = [
                t for t in self._tracks
                if t.hits >= self.min_hits
            ]
            if not confirmed:
                return None
            return max(confirmed, key=lambda t: t.hits)

    def clear(self) -> None:
        """Remove all tracks."""
        with self._lock:
            self._tracks.clear()
            self._next_id = 1
            self._last_predict_time = 0.0

    @property
    def track_count(self) -> int:
        """Number of active tracks (including unconfirmed)."""
        with self._lock:
            return len(self._tracks)

    @property
    def confirmed_count(self) -> int:
        """Number of confirmed tracks (hits >= min_hits)."""
        with self._lock:
            return sum(1 for t in self._tracks if t.hits >= self.min_hits)

    def _create_track(self, det: Detection) -> KalmanTrack:
        track = KalmanTrack(
            track_id=self._next_id,
            width=det.width,
            height=det.height,
            confidence=det.confidence,
        )
        track.init_state(det.cx, det.cy)
        self._tracks.append(track)
        self._next_id += 1
        return track

    def _prune_and_return(self) -> list[KalmanTrack]:
        """Remove stale tracks and return confirmed ones."""
        self._tracks = [
            t for t in self._tracks
            if t.time_since_update < self.max_age
        ]
        return [t for t in self._tracks if t.hits >= self.min_hits]
