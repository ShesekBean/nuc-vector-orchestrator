"""YOLO11s person detector using OpenVINO inference on NUC.

Loads a YOLO model (preferring OpenVINO IR format for Intel hardware) and
detects persons (COCO class 0) in camera frames.  Designed to consume
frames from ``CameraClient`` and feed detections into ``KalmanTracker``.

Architecture::

    CameraClient.get_latest_frame()  ──►  PersonDetector.detect(frame)
                                                │
                                                ├──► list[Detection]  ──► KalmanTracker.update()
                                                └──► NucEventBus  (YOLO_PERSON_DETECTED)
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from apps.vector.src.detector.kalman_tracker import Detection
from apps.vector.src.events.event_types import (
    YOLO_PERSON_DETECTED,
    YoloPersonDetectedEvent,
)

import numpy as np

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# COCO class index for "person"
_PERSON_CLASS = 0

# Default model directory (relative to repo root)
_DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "models", "yolo11s_openvino_model"
)


class PersonDetector:
    """Detect persons in camera frames using YOLO + OpenVINO.

    Args:
        model_path: Path to YOLO model (OpenVINO IR dir or .pt file).
            Defaults to ``apps/vector/models/yolo11s_openvino_model/``.
        confidence_threshold: Minimum detection confidence (0.0–1.0).
        iou_threshold: NMS IoU threshold for suppressing overlapping boxes.
        event_bus: Optional ``NucEventBus`` to publish detections on.
    """

    def __init__(
        self,
        model_path: str | None = None,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        event_bus: NucEventBus | None = None,
    ) -> None:
        self._model_path = model_path or os.path.normpath(_DEFAULT_MODEL_DIR)
        self._confidence_threshold = confidence_threshold
        self._iou_threshold = iou_threshold
        self._event_bus = event_bus

        self._model = None  # Lazy-loaded YOLO model
        self._frame_count = 0
        self._total_inference_ms = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_model(self) -> None:
        """Eagerly load the YOLO model.  Called automatically on first detect()."""
        if self._model is not None:
            return

        from ultralytics import YOLO

        if not os.path.exists(self._model_path):
            raise FileNotFoundError(
                f"YOLO model not found at {self._model_path}. "
                "Run: python3 scripts/export-openvino-models.py"
            )

        logger.info("Loading YOLO model from %s", self._model_path)
        self._model = YOLO(self._model_path)
        logger.info("YOLO model loaded successfully")

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Run person detection on a single BGR frame.

        Args:
            frame: BGR numpy array (800x600 from Vector camera).

        Returns:
            List of ``Detection`` objects for persons found in the frame.
        """
        if self._model is None:
            self.load_model()

        # Compute adaptive threshold on ORIGINAL frame (before CLAHE modifies brightness)
        conf = self._adaptive_threshold(frame, self._confidence_threshold)

        # Multi-stage low-light preprocessing for Vector's dark OV7251 camera
        enhanced = self._enhance_low_light(frame)

        t0 = time.perf_counter()
        results = self._model(
            enhanced,
            verbose=False,
            conf=conf,
            iou=self._iou_threshold,
            classes=[_PERSON_CLASS],
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self._frame_count += 1
        self._total_inference_ms += elapsed_ms

        detections = self._parse_results(results, frame.shape)
        self._publish_detections(detections, frame.shape)

        return detections

    @property
    def confidence_threshold(self) -> float:
        return self._confidence_threshold

    @confidence_threshold.setter
    def confidence_threshold(self, value: float) -> None:
        self._confidence_threshold = max(0.0, min(1.0, value))

    @property
    def fps(self) -> float:
        """Average inference FPS over all frames processed."""
        if self._frame_count == 0 or self._total_inference_ms == 0:
            return 0.0
        return self._frame_count / (self._total_inference_ms / 1000.0)

    @property
    def avg_inference_ms(self) -> float:
        """Average per-frame inference time in milliseconds."""
        if self._frame_count == 0:
            return 0.0
        return self._total_inference_ms / self._frame_count

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    # Brightness breakpoints for adaptive confidence scaling
    _DARK_BRIGHTNESS = 60
    _BRIGHT_BRIGHTNESS = 150
    _DARK_CONF_SCALE = 0.6  # multiply base threshold (e.g. 0.25 * 0.6 = 0.15)
    _BRIGHT_CONF_SCALE = 1.4  # multiply base threshold (e.g. 0.25 * 1.4 = 0.35)

    @staticmethod
    def _enhance_low_light(frame: np.ndarray) -> np.ndarray:
        """Multi-stage low-light enhancement for Vector's dark OV7251 camera.

        Pipeline (applied adaptively based on frame brightness):
        1. Gamma correction (brightens dark regions non-linearly)
        2. CLAHE on L channel (adaptive contrast enhancement)
        3. Fast denoising (reduces noise amplified by steps 1-2)

        In very dark frames (<40 mean brightness), aggressive gamma is
        applied first. In moderate frames, only CLAHE is used.
        """
        import cv2

        # Measure frame brightness to adapt pipeline
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_brightness = gray.mean()

        result = frame

        # Stage 1: Gamma correction for very dark frames
        # gamma < 1.0 brightens; more aggressive when darker
        if mean_brightness < 80:
            if mean_brightness < 30:
                gamma = 0.35  # extremely dark — aggressive brightening
            elif mean_brightness < 50:
                gamma = 0.45  # very dark
            else:
                gamma = 0.6  # moderately dark
            table = (((np.arange(256) / 255.0) ** gamma) * 255).astype(np.uint8)
            result = cv2.LUT(result, table)

        # Stage 2: CLAHE on L channel (adaptive histogram equalization)
        lab = cv2.cvtColor(result, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)

        # Higher clipLimit for darker frames (more contrast boost needed)
        clip_limit = 4.0 if mean_brightness < 50 else 3.0
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
        l_enhanced = clahe.apply(l_channel)

        lab_enhanced = cv2.merge([l_enhanced, a_channel, b_channel])
        result = cv2.cvtColor(lab_enhanced, cv2.COLOR_LAB2BGR)

        # Stage 3: Fast denoising to reduce noise from gamma + CLAHE
        # Only apply when we did aggressive brightening (noise is amplified)
        if mean_brightness < 50:
            result = cv2.fastNlMeansDenoisingColored(
                result, None, h=6, hColor=6,
                templateWindowSize=7, searchWindowSize=21,
            )

        return result

    @staticmethod
    def _adaptive_threshold(
        frame: np.ndarray, base_threshold: float
    ) -> float:
        """Scale confidence threshold based on frame mean brightness.

        Computes mean brightness from the grayscale frame (fast O(n) op,
        ~0.1ms for 800x600).  Linearly interpolates a scale factor:

        - Dark frames (mean < 60): scale = 0.6  -> 0.25 * 0.6 = 0.15
        - Normal frames (60-150): linearly interpolate from 0.6 to 1.4
        - Bright frames (mean > 150): scale = 1.4 -> 0.25 * 1.4 = 0.35

        This complements CLAHE -- dark frames get more permissive detection
        (CLAHE amplifies noise, but real detections still come through),
        while bright frames get stricter thresholds to reduce false positives.
        """
        import cv2

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_brightness = float(gray.mean())

        dark = PersonDetector._DARK_BRIGHTNESS
        bright = PersonDetector._BRIGHT_BRIGHTNESS
        dark_scale = PersonDetector._DARK_CONF_SCALE
        bright_scale = PersonDetector._BRIGHT_CONF_SCALE

        if mean_brightness <= dark:
            scale = dark_scale
        elif mean_brightness >= bright:
            scale = bright_scale
        else:
            # Linear interpolation between dark and bright breakpoints
            t = (mean_brightness - dark) / (bright - dark)
            scale = dark_scale + t * (bright_scale - dark_scale)

        return max(0.01, min(0.99, base_threshold * scale))

    def _parse_results(
        self, results: list, frame_shape: tuple[int, ...]
    ) -> list[Detection]:
        """Extract person detections from ultralytics results."""
        detections: list[Detection] = []

        if not results or len(results) == 0:
            return detections

        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return detections

        for box in boxes:
            cls_id = int(box.cls[0])
            if cls_id != _PERSON_CLASS:
                continue

            conf = float(box.conf[0])
            # xyxy format: [x1, y1, x2, y2]
            x1, y1, x2, y2 = box.xyxy[0].tolist()

            # Convert to center format for Detection / KalmanTracker
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            w = x2 - x1
            h = y2 - y1

            detections.append(Detection(cx=cx, cy=cy, width=w, height=h, confidence=conf))

        return detections

    def _publish_detections(
        self, detections: list[Detection], frame_shape: tuple[int, ...]
    ) -> None:
        """Emit detection events on the NucEventBus."""
        if self._event_bus is None:
            return

        frame_h, frame_w = frame_shape[0], frame_shape[1]

        for det in detections:
            event = YoloPersonDetectedEvent(
                x=det.cx,
                y=det.cy,
                width=det.width,
                height=det.height,
                confidence=det.confidence,
                frame_width=frame_w,
                frame_height=frame_h,
            )
            self._event_bus.emit(YOLO_PERSON_DETECTED, event)
