"""Background presence detection loop — drives the companion system.

Continuously grabs camera frames at a low rate and runs person detection
+ face recognition, emitting YOLO_PERSON_DETECTED and FACE_RECOGNIZED
events that PresenceTracker already subscribes to.

Adaptive rate: 1 fps when someone is present (responsive to movement),
0.2 fps when idle (saves CPU).  Auto-pauses when the follow pipeline is
active (follow already runs detection at ~20 Hz).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import FOLLOW_STATE_CHANGED

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.detector.person_detector import PersonDetector
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.face_recognition.face_detector import FaceDetector
    from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer

logger = logging.getLogger(__name__)

# Detection intervals (seconds)
_INTERVAL_PERSON_PRESENT_S = 1.0  # 1 fps when someone is here
_INTERVAL_IDLE_S = 5.0  # 0.2 fps when nobody around


class PresenceDetectionLoop:
    """Background thread that feeds camera frames into detection pipelines.

    This is the missing link between the camera and the companion system.
    Without it, YOLO_PERSON_DETECTED and FACE_RECOGNIZED events never fire
    unless /follow/start or /patrol/start is active.

    Parameters
    ----------
    camera_client : CameraClient
        Source of camera frames (already buffering at ~15 fps).
    person_detector : PersonDetector
        Shared YOLO detector instance (same one used by follow).
    face_detector : FaceDetector
        YuNet face detector (lightweight, lazy-loads model on first call).
    face_recognizer : FaceRecognizer
        SFace recognizer with enrollment database.
    event_bus : NucEventBus
        For subscribing to FOLLOW_STATE_CHANGED.
    """

    def __init__(
        self,
        camera_client: CameraClient,
        person_detector: PersonDetector,
        face_detector: FaceDetector,
        face_recognizer: FaceRecognizer,
        event_bus: NucEventBus,
    ) -> None:
        self._camera = camera_client
        self._person_detector = person_detector
        self._face_detector = face_detector
        self._face_recognizer = face_recognizer
        self._bus = event_bus

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._paused = False  # True when follow pipeline is active

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the background detection loop."""
        if self._thread is not None and self._thread.is_alive():
            return

        # Load face database so recognition works immediately
        try:
            self._face_recognizer.load_database()
            logger.info("Face database loaded for presence detection")
        except Exception:
            logger.warning("Failed to load face database — recognition disabled until retry")

        self._stop_event.clear()
        self._paused = False
        self._bus.on(FOLLOW_STATE_CHANGED, self._on_follow_state)

        self._thread = threading.Thread(
            target=self._loop, name="presence-detection", daemon=True
        )
        self._thread.start()
        logger.info("PresenceDetectionLoop started")

    def stop(self) -> None:
        """Stop the background detection loop."""
        self._stop_event.set()
        self._bus.off(FOLLOW_STATE_CHANGED, self._on_follow_state)
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("PresenceDetectionLoop stopped")

    def is_alive(self) -> bool:
        """Health check for supervisor."""
        return self._thread is not None and self._thread.is_alive()

    # -- Event handlers ------------------------------------------------------

    def _on_follow_state(self, event: Any) -> None:
        """Pause/resume when follow pipeline starts/stops."""
        state = getattr(event, "state", None)
        if state in ("searching", "tracking", "following"):
            if not self._paused:
                self._paused = True
                logger.info("PresenceDetectionLoop paused — follow active")
        else:
            if self._paused:
                self._paused = False
                logger.info("PresenceDetectionLoop resumed — follow idle")

    # -- Main loop -----------------------------------------------------------

    def _loop(self) -> None:
        """Core detection loop — runs until stop_event is set."""
        logger.debug("Presence detection loop running")

        while not self._stop_event.is_set():
            # Skip while follow pipeline handles detection
            if self._paused:
                self._stop_event.wait(timeout=1.0)
                continue

            # Grab latest frame from camera buffer
            frame = self._camera.get_latest_frame()
            if frame is None:
                self._stop_event.wait(timeout=1.0)
                continue

            interval = _INTERVAL_IDLE_S
            try:
                from apps.vector.src.events.event_types import (
                    YOLO_PERSON_DETECTED, FACE_RECOGNIZED,
                    YoloPersonDetectedEvent, FaceRecognizedEvent,
                )

                detections = self._person_detector.detect(frame)

                if detections:
                    interval = _INTERVAL_PERSON_PRESENT_S

                    # Emit person detected events
                    for det in detections:
                        self._bus.emit(YOLO_PERSON_DETECTED, YoloPersonDetectedEvent(
                            x=det.bbox[0], y=det.bbox[1],
                            width=det.bbox[2], height=det.bbox[3],
                            confidence=det.confidence,
                        ))

                    # Face detection + recognition only when person found
                    try:
                        faces = self._face_detector.detect(frame)
                        if faces:
                            results = self._face_recognizer.recognize(frame, faces)
                            if results:
                                for name, conf, bbox in results:
                                    self._bus.emit(FACE_RECOGNIZED, FaceRecognizedEvent(
                                        name=name, confidence=conf,
                                        x=bbox[0], y=bbox[1],
                                        width=bbox[2], height=bbox[3],
                                    ))
                    except Exception:
                        logger.debug("Face detection/recognition error", exc_info=True)

            except Exception:
                logger.debug("Person detection error", exc_info=True)
                interval = _INTERVAL_IDLE_S

            self._stop_event.wait(timeout=interval)
