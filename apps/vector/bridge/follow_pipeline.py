"""Follow pipeline — wires YOLO detection → FollowPlanner (no Kalman).

Runs a detection loop thread that:
1. Grabs frames from CameraClient
2. Runs YOLO person detection
3. Picks the best detection and emits TrackedPersonEvent directly
4. FollowPlanner subscribes and drives motors

Simplified from the original Kalman-based pipeline. YOLO at ~15-20fps
on NUC OpenVINO is fast enough — no need for Kalman smoothing.

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
from typing import TYPE_CHECKING, Any

from apps.vector.src.detector.person_detector import PersonDetector
from apps.vector.src.events.event_types import TRACKED_PERSON, TrackedPersonEvent
from apps.vector.src.planner.follow_planner import FollowPlanner

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.head_controller import HeadController
    from apps.vector.src.motor_controller import MotorController

logger = logging.getLogger(__name__)

# Detection loop rate — YOLO11n runs at ~20fps on NUC with OpenVINO
DETECTION_HZ = 20.0

# Track ID counter — simple incrementing ID for each new detection
_next_track_id = 0


class FollowPipeline:
    """End-to-end person following pipeline.

    Owns the PersonDetector and FollowPlanner instances.
    Runs a detection loop that bridges camera frames → YOLO → planner.
    No Kalman tracker — YOLO detections are passed directly to the planner.
    """

    def __init__(
        self,
        camera_client: CameraClient,
        motor_controller: MotorController,
        head_controller: HeadController,
        nuc_bus: NucEventBus,
        robot: Any = None,
    ) -> None:
        self._camera = camera_client
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus
        self._robot = robot

        self._detector = PersonDetector(event_bus=nuc_bus)

        # Build say_func for voice feedback if robot is available
        say_func = None
        if robot is not None:
            def _say(text: str) -> None:
                try:
                    robot.behavior.say_text(text)
                except Exception:
                    logger.warning("say_text failed: %s", text)
            say_func = _say

        self._planner = FollowPlanner(
            motor_controller, head_controller, nuc_bus,
            say_func=say_func,
        )

        self._running = False
        self._thread: threading.Thread | None = None

        # Simple track ID — keeps same ID while continuously detecting,
        # increments when detection gap > 5 frames
        self._current_track_id = 0
        self._frames_without_detection = 0

    @property
    def is_active(self) -> bool:
        return self._running

    @property
    def state(self) -> str:
        return self._planner.state.value

    def start(self) -> None:
        """Start the full follow pipeline (YOLO + planner)."""
        if self._running:
            logger.warning("FollowPipeline already running")
            return

        # Boost camera exposure for low-light following
        self._boost_camera_exposure()

        # Load YOLO model (first call may take a few seconds)
        logger.info("Loading YOLO model for person detection...")
        self._detector.load_model()
        logger.info("YOLO model loaded (avg inference: %.1fms)", self._detector.avg_inference_ms)

        self._running = True
        self._frames_without_detection = 0
        self._current_track_id = 0

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

        # Stop detection loop
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        # Restore auto exposure
        self._restore_camera_exposure()

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
        }

    def _detection_loop(self) -> None:
        """Grab frames, run YOLO, emit best detection as TrackedPersonEvent."""
        global _next_track_id
        period = 1.0 / DETECTION_HZ

        while self._running:
            loop_start = time.monotonic()

            try:
                # Get latest frame from camera
                frame = self._camera.get_latest_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue

                frame_h, frame_w = frame.shape[:2]

                # Run YOLO detection
                detections = self._detector.detect(frame)

                if detections:
                    # Pick the best detection (highest confidence)
                    best = max(detections, key=lambda d: d.confidence)

                    # Reset gap counter
                    if self._frames_without_detection > 5:
                        # Gap was long enough — assign new track ID
                        _next_track_id += 1
                        self._current_track_id = _next_track_id
                    self._frames_without_detection = 0

                    logger.debug(
                        "YOLO: %d person(s), best conf=%.2f cx=%.0f cy=%.0f w=%.0f h=%.0f",
                        len(detections), best.confidence, best.cx, best.cy, best.width, best.height,
                    )

                    # Emit directly as TrackedPersonEvent (no Kalman)
                    event = TrackedPersonEvent(
                        track_id=self._current_track_id,
                        cx=best.cx,
                        cy=best.cy,
                        width=best.width,
                        height=best.height,
                        age_frames=0,
                        hits=1,
                        confidence=best.confidence,
                        frame_width=frame_w,
                        frame_height=frame_h,
                    )
                    self._bus.emit(TRACKED_PERSON, event)
                else:
                    self._frames_without_detection += 1

            except Exception:
                logger.exception("Error in detection loop")

            # Rate limit
            elapsed = time.monotonic() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _boost_camera_exposure(self) -> None:
        """Ensure camera auto-exposure is enabled for best low-light performance."""
        if self._robot is None:
            return
        try:
            self._robot.camera.enable_auto_exposure()
            logger.info("Camera auto-exposure enabled for follow pipeline")
        except Exception:
            logger.warning("Failed to enable auto-exposure", exc_info=True)

    def _restore_camera_exposure(self) -> None:
        """Restore camera to auto-exposure after pipeline stops."""
        if self._robot is None:
            return
        try:
            self._robot.camera.enable_auto_exposure()
            logger.debug("Camera auto-exposure restored")
        except Exception:
            logger.warning("Failed to restore auto-exposure", exc_info=True)
