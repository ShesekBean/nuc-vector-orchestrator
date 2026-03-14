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
import os
import threading
import time
from typing import TYPE_CHECKING, Any

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

# Detection loop rate — YOLO11n runs at ~20fps on NUC with OpenVINO
# Run as fast as possible; actual rate limited by inference time (~47ms)
DETECTION_HZ = 20.0


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
        robot: Any = None,
    ) -> None:
        self._camera = camera_client
        self._motor = motor_controller
        self._head = head_controller
        self._bus = nuc_bus
        self._robot = robot

        self._detector = PersonDetector(event_bus=nuc_bus)
        self._tracker = KalmanTracker()
        self._obstacle = ObstacleDetector(motor_controller, nuc_bus)

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
            obstacle_detector=self._obstacle,
            say_func=say_func,
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

        # Request behavior control — needed for motors and say_text
        self._request_control()

        # Set head to neutral position
        try:
            self._head.set_angle(10.0)
        except Exception:
            logger.warning("Failed to set head to neutral on follow start")

        # Boost camera exposure for low-light following
        self._boost_camera_exposure()

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

        # Restore auto exposure
        self._restore_camera_exposure()

        # Release behavior control and re-send quiet intent
        self._release_control()

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
                    best = max(detections, key=lambda d: d.confidence)
                    logger.debug(
                        "YOLO: %d person(s), best conf=%.2f cx=%.0f cy=%.0f w=%.0f h=%.0f",
                        len(detections), best.confidence, best.cx, best.cy, best.width, best.height,
                    )

                # Feed into Kalman tracker
                confirmed_tracks = self._tracker.update(detections)

                # Only emit events when YOLO actually detected something.
                # Kalman predictions without fresh YOLO data are stale —
                # let the planner's lost-frames counter handle the gap.
                if detections and confirmed_tracks:
                    primary = self._tracker.get_primary_track()
                    if primary:
                        logger.debug(
                            "Kalman: track_id=%d hits=%d cx=%.0f cy=%.0f h=%.0f conf=%.2f",
                            primary.track_id, primary.hits, primary.cx, primary.cy, primary.height, primary.confidence,
                        )
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

    def _request_control(self) -> None:
        """Request SDK behavior control for motor/speech access."""
        if self._robot is None:
            return
        try:
            from anki_vector.connection import ControlPriorityLevel
            self._robot.conn.request_control(
                behavior_control_level=ControlPriorityLevel.OVERRIDE_BEHAVIORS_PRIORITY,
            )
            logger.info("Behavior control acquired for follow pipeline")
        except Exception:
            logger.exception("Failed to request behavior control")

    def _release_control(self) -> None:
        """Release SDK behavior control and re-send quiet intent."""
        if self._robot is None:
            return
        try:
            self._robot.conn.release_control()
            logger.info("Behavior control released after follow pipeline")
        except Exception:
            logger.exception("Failed to release behavior control")
        # Re-send quiet intent so Vector sits still after follow
        self._send_quiet_intent()

    def _send_quiet_intent(self) -> None:
        """Send imperative_quiet intent via wire-pod."""
        import urllib.request
        import urllib.parse

        serial = os.environ.get("VECTOR_SERIAL", "0dd1cdcf")
        try:
            url = "http://localhost:8080/api-sdk/cloud_intent?" + urllib.parse.urlencode({
                "serial": serial,
                "intent": "intent_imperative_quiet",
            })
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
            logger.info("Sent quiet intent after follow stop")
        except Exception:
            logger.warning("Failed to send quiet intent", exc_info=True)

    def _boost_camera_exposure(self) -> None:
        """Ensure camera auto-exposure is enabled for best low-light performance.

        Testing showed Vector's auto-exposure (OV7251) already optimizes for
        available light.  Manual exposure settings produced darker frames than
        auto, so we just make sure auto-exposure is active.
        """
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
