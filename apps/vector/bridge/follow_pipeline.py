"""Follow pipeline — wires YOLO detection → Kalman tracking → FollowPlanner.

Runs a detection loop thread that:
1. Grabs frames from CameraClient
2. Runs YOLO person detection
3. Feeds detections into KalmanTracker
4. Emits TrackedPersonEvent on the NucEventBus
5. FollowPlanner subscribes and drives motors

Usage::

    pipeline = FollowPipeline(camera_client, motor_controller,
                              head_controller, nuc_bus)
    pipeline.start()   # loads YOLO, starts detection + follow
    pipeline.stop()    # stops everything, releases motors
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from apps.vector.src.detector.kalman_tracker import KalmanTracker
from apps.vector.src.detector.person_detector import PersonDetector
from apps.vector.src.events.event_types import TRACKED_PERSON, TrackedPersonEvent
from apps.vector.src.planner.follow_planner import FollowPlanner
from apps.vector.src.planner.obstacle_detector import ObstacleDetector

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.motor_controller import MotorController

logger = logging.getLogger(__name__)

# Detection loop rate — YOLO runs at ~5-15fps on NUC with OpenVINO
DETECTION_HZ = 5.0


class FollowPipeline:
    """End-to-end person following pipeline.

    Owns the PersonDetector, KalmanTracker, and FollowPlanner instances.
    Runs a detection loop that bridges camera frames → YOLO → Kalman → planner.
    """

    def __init__(
        self,
        camera_client: CameraClient,
        motor_controller: MotorController,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
    ) -> None:
        self._camera = camera_client
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus

        self._detector = PersonDetector(event_bus=nuc_bus)
        self._tracker = KalmanTracker()
        self._obstacle = ObstacleDetector(motor_controller, nuc_bus)
        self._planner = FollowPlanner(
            motor_controller, head_controller, nuc_bus,
            obstacle_detector=self._obstacle,
        )

        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def is_active(self) -> bool:
        return self._running

    @property
    def state(self) -> str:
        return self._planner.state.value

    def start(self) -> None:
        """Start the full follow pipeline (YOLO + Kalman + planner)."""
        if self._running:
            logger.warning("FollowPipeline already running")
            return

        # Load YOLO model (first call may take a few seconds)
        logger.info("Loading YOLO model for person detection...")
        self._detector.load_model()
        logger.info("YOLO model loaded (avg inference: %.1fms)", self._detector.avg_inference_ms)

        self._running = True
        self._tracker.clear()

        # Start obstacle detector (listens for motor events)
        self._obstacle.start()

        # Start detection loop thread
        self._thread = threading.Thread(
            target=self._detection_loop, name="follow-detection", daemon=True
        )
        self._thread.start()

        # Start the follow planner (subscribes to TRACKED_PERSON events)
        self._planner.start()
        logger.info("FollowPipeline started")

    def stop(self) -> None:
        """Stop the pipeline — stops planner, detection loop, and motors."""
        if not self._running:
            return

        self._running = False

        # Stop planner first (stops motors)
        self._planner.stop()

        # Stop obstacle detector
        self._obstacle.stop()

        # Stop detection loop
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        self._tracker.clear()
        logger.info("FollowPipeline stopped")

    def get_status(self) -> dict:
        """Return pipeline status and diagnostics as a dict."""
        if not self._running:
            return {"active": False}

        return {
            "active": True,
            "state": self._planner.state.value,
            "locked_track_id": self._planner.locked_track_id,
            "detector": {
                "fps": round(self._detector.fps, 1),
                "avg_inference_ms": round(self._detector.avg_inference_ms, 1),
                "frame_count": self._detector.frame_count,
            },
            "tracker": {
                "track_count": self._tracker.track_count,
                "confirmed_count": self._tracker.confirmed_count,
            },
            "obstacle": {
                "zone": self._obstacle.zone,
                "speed_scale": round(self._obstacle.speed_scale, 2),
                "escape_count": self._obstacle.escape_count,
            },
        }

    def _detection_loop(self) -> None:
        """Grab frames, run YOLO, feed Kalman tracker, emit events."""
        period = 1.0 / DETECTION_HZ

        while self._running:
            loop_start = time.monotonic()

            try:
                # Get latest frame from camera
                frame = self._camera.get_latest_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue

                # Run YOLO detection
                detections = self._detector.detect(frame)

                if detections:
                    logger.debug(
                        "YOLO: %d person(s), best conf=%.2f",
                        len(detections), max(d.confidence for d in detections),
                    )

                # Feed into Kalman tracker
                confirmed_tracks = self._tracker.update(detections)

                if confirmed_tracks:
                    primary = self._tracker.get_primary_track()
                    if primary:
                        logger.debug(
                            "Kalman: track_id=%d hits=%d cx=%.0f cy=%.0f h=%.0f",
                            primary.track_id, primary.hits, primary.cx, primary.cy, primary.height,
                        )

                # Emit TrackedPersonEvent for the primary (best) track
                if confirmed_tracks:
                    primary = self._tracker.get_primary_track()
                    if primary is not None:
                        event = TrackedPersonEvent(
                            track_id=primary.track_id,
                            cx=primary.cx,
                            cy=primary.cy,
                            width=primary.width,
                            height=primary.height,
                            age_frames=primary.age,
                            hits=primary.hits,
                            confidence=primary.confidence,
                        )
                        self._bus.emit(TRACKED_PERSON, event)

            except Exception:
                logger.exception("Error in detection loop")

            # Rate limit
            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
