"""YuNet face detector using OpenCV DNN with OpenVINO backend.

Wraps cv2.FaceDetectorYN for face detection on Vector camera frames.
Lazy-loads the model on first detect() call so CI environments without
cv2 or model files can still import the module.

R3 reference: confidence range 0.54–0.78, threshold 0.363.
Vector improvement: 120° FOV (less distortion than R3's 160° fisheye).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Default model path relative to this file
_DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "models",
)
_YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"


@dataclass(frozen=True)
class FaceDetection:
    """A single detected face with bounding box and landmarks."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    # 5-point landmarks: right_eye, left_eye, nose, right_mouth, left_mouth
    landmarks: tuple[tuple[float, float], ...] = ()


class FaceDetector:
    """YuNet face detector using OpenCV DNN (OpenVINO backend when available).

    Args:
        model_path: Path to YuNet ONNX model. Defaults to models/ directory.
        confidence_threshold: Minimum detection confidence (R3 used 0.5).
        nms_threshold: Non-maximum suppression threshold.
        top_k: Maximum detections before NMS.
        input_size: Model input size (width, height).
    """

    def __init__(
        self,
        model_path: str | None = None,
        confidence_threshold: float = 0.5,
        nms_threshold: float = 0.3,
        top_k: int = 5000,
        input_size: tuple[int, int] = (320, 320),
    ) -> None:
        if model_path is None:
            model_path = os.path.join(
                os.path.abspath(_DEFAULT_MODEL_DIR), _YUNET_FILENAME,
            )
        self._model_path = model_path
        self._confidence_threshold = confidence_threshold
        self._nms_threshold = nms_threshold
        self._top_k = top_k
        self._input_size = input_size
        self._detector = None  # lazy-loaded

    def _load_model(self) -> None:
        """Lazy-load the YuNet model via OpenCV."""
        import cv2

        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(
                f"YuNet model not found: {self._model_path}. "
                "Run: python3 scripts/export-openvino-models.py"
            )

        self._detector = cv2.FaceDetectorYN.create(
            model=self._model_path,
            config="",
            input_size=self._input_size,
            score_threshold=self._confidence_threshold,
            nms_threshold=self._nms_threshold,
            top_k=self._top_k,
        )

        # Try OpenVINO backend (falls back to default if unavailable)
        try:
            self._detector.setPreferableBackend(cv2.dnn.DNN_BACKEND_INFERENCE_ENGINE)
            self._detector.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            logger.info("YuNet using OpenVINO backend")
        except Exception:
            logger.info("YuNet using default OpenCV DNN backend")

        logger.info(
            "YuNet loaded: model=%s, input_size=%s, threshold=%.2f",
            os.path.basename(self._model_path),
            self._input_size,
            self._confidence_threshold,
        )

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        """Detect faces in a BGR frame.

        Args:
            frame: BGR image as numpy array (H, W, 3).

        Returns:
            List of FaceDetection objects sorted by confidence (highest first).
        """
        if self._detector is None:
            self._load_model()

        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))

        _, faces = self._detector.detect(frame)

        if faces is None:
            return []

        detections = []
        for face in faces:
            # YuNet output: [x, y, w, h, x_re, y_re, x_le, y_le,
            #                 x_nose, y_nose, x_rm, y_rm, x_lm, y_lm, score]
            fx, fy, fw, fh = float(face[0]), float(face[1]), float(face[2]), float(face[3])
            score = float(face[14])

            landmarks = (
                (float(face[4]), float(face[5])),    # right eye
                (float(face[6]), float(face[7])),    # left eye
                (float(face[8]), float(face[9])),    # nose
                (float(face[10]), float(face[11])),  # right mouth
                (float(face[12]), float(face[13])),  # left mouth
            )

            detections.append(FaceDetection(
                x=fx, y=fy, width=fw, height=fh,
                confidence=score, landmarks=landmarks,
            ))

        # Sort by confidence descending
        detections.sort(key=lambda d: d.confidence, reverse=True)
        return detections

    @property
    def confidence_threshold(self) -> float:
        return self._confidence_threshold

    @confidence_threshold.setter
    def confidence_threshold(self, value: float) -> None:
        self._confidence_threshold = value
        if self._detector is not None:
            self._detector.setScoreThreshold(value)

    @property
    def is_loaded(self) -> bool:
        return self._detector is not None
