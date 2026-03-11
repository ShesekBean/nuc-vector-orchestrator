"""Scene description pipeline: camera frame + YOLO detection + LLM description.

Captures a frame from Vector's camera, runs YOLO object detection for an
inventory of visible objects, draws annotated bounding boxes, and sends the
frame to Claude Vision for a natural language scene description.

All heavy dependencies (ultralytics, cv2, anthropic) are lazy-imported so the
module can be imported in CI without those packages installed.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Default YOLO model search paths (relative to apps/vector/models/)
_MODEL_SEARCH = [
    "yolo11s_openvino_model",
    "yolo11s.pt",
    "yolov8n.pt",
    "yolo11s.onnx",
    "yolov8n.onnx",
]

# Default LLM model for scene description (cost-effective vision model)
_DEFAULT_LLM_MODEL = "claude-sonnet-4-20250514"

# YOLO confidence threshold
_DEFAULT_CONFIDENCE = 0.3


@dataclass
class ObjectDetection:
    """A single detected object from YOLO inference."""

    label: str
    confidence: float
    x: int  # top-left x
    y: int  # top-left y
    width: int
    height: int


@dataclass
class SceneResult:
    """Complete result from a scene description capture."""

    frame_jpeg: bytes
    annotated_jpeg: bytes
    detections: list[ObjectDetection] = field(default_factory=list)
    description: str = ""
    timestamp: float = 0.0


class SceneDescriber:
    """Captures frames, runs YOLO detection, and generates LLM scene descriptions.

    Args:
        camera_client: Connected CameraClient for frame capture.
        event_bus: Optional NucEventBus for emitting SceneDescriptionEvent.
        model_dir: Directory containing YOLO models.
            Defaults to ``apps/vector/models/``.
        yolo_confidence: Minimum YOLO confidence threshold.
        llm_model: Claude model ID for vision descriptions.
    """

    def __init__(
        self,
        camera_client: CameraClient,
        event_bus: NucEventBus | None = None,
        model_dir: str | Path | None = None,
        yolo_confidence: float = _DEFAULT_CONFIDENCE,
        llm_model: str = _DEFAULT_LLM_MODEL,
    ) -> None:
        self._camera = camera_client
        self._bus = event_bus
        self._model_dir = Path(model_dir) if model_dir else self._default_model_dir()
        self._yolo_confidence = yolo_confidence
        self._llm_model = llm_model

        self._yolo_model: object | None = None
        self._yolo_loaded = False

    @staticmethod
    def _default_model_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "models"

    def capture_scene(self) -> SceneResult:
        """Full pipeline: capture frame, detect objects, describe scene.

        Returns:
            SceneResult with frame, annotated image, detections, and description.

        Raises:
            RuntimeError: If no frame is available from the camera.
        """

        frame = self._camera.get_latest_frame()
        jpeg_bytes = self._camera.get_latest_jpeg()

        if frame is None or jpeg_bytes is None:
            raise RuntimeError("No frame available from camera")

        timestamp = time.time()
        detections = self._run_yolo(frame)
        annotated_jpeg = self._annotate_frame(frame, detections)
        description = self._describe_with_llm(jpeg_bytes, detections)

        result = SceneResult(
            frame_jpeg=jpeg_bytes,
            annotated_jpeg=annotated_jpeg,
            detections=detections,
            description=description,
            timestamp=timestamp,
        )

        if self._bus:
            from apps.vector.src.events.event_types import (
                SCENE_DESCRIPTION,
                SceneDescriptionEvent,
            )

            self._bus.emit(
                SCENE_DESCRIPTION,
                SceneDescriptionEvent(
                    description=description,
                    detection_count=len(detections),
                    detection_labels=tuple(d.label for d in detections),
                    timestamp=timestamp,
                ),
            )

        return result

    def get_annotated_frame(
        self, frame: np.ndarray, detections: list[ObjectDetection]
    ) -> bytes:
        """Draw detection bounding boxes on a frame and return JPEG bytes."""
        return self._annotate_frame(frame, detections)

    def detect_objects(self, frame: np.ndarray) -> list[ObjectDetection]:
        """Run YOLO on a frame and return detections."""
        return self._run_yolo(frame)

    def describe_frame(
        self, jpeg_bytes: bytes, detections: list[ObjectDetection]
    ) -> str:
        """Send a frame + detections to Claude Vision for a description."""
        return self._describe_with_llm(jpeg_bytes, detections)

    # ------------------------------------------------------------------
    # YOLO detection
    # ------------------------------------------------------------------

    def _load_yolo(self) -> object | None:
        """Lazy-load YOLO model. Returns None if no model available."""
        if self._yolo_loaded:
            return self._yolo_model

        self._yolo_loaded = True

        try:
            from ultralytics import YOLO
        except ImportError:
            logger.warning("ultralytics not installed — YOLO detection disabled")
            return None

        for name in _MODEL_SEARCH:
            path = self._model_dir / name
            if path.exists():
                logger.info("Loading YOLO model: %s", path)
                self._yolo_model = YOLO(str(path))
                return self._yolo_model

        logger.warning("No YOLO model found in %s — detection disabled", self._model_dir)
        return None

    def _run_yolo(self, frame: np.ndarray) -> list[ObjectDetection]:
        """Run YOLO inference and return ObjectDetection list."""
        model = self._load_yolo()
        if model is None:
            return []

        try:
            results = model(frame, conf=self._yolo_confidence, verbose=False)
        except Exception:
            logger.exception("YOLO inference failed")
            return []

        detections: list[ObjectDetection] = []
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy()
                cls_id = int(boxes.cls[i].cpu().numpy())
                conf = float(boxes.conf[i].cpu().numpy())
                label = result.names.get(cls_id, f"class_{cls_id}")

                x1, y1, x2, y2 = int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])
                detections.append(
                    ObjectDetection(
                        label=label,
                        confidence=conf,
                        x=x1,
                        y=y1,
                        width=x2 - x1,
                        height=y2 - y1,
                    )
                )

        logger.info("YOLO detected %d objects", len(detections))
        return detections

    # ------------------------------------------------------------------
    # Frame annotation
    # ------------------------------------------------------------------

    def _annotate_frame(
        self, frame: np.ndarray, detections: list[ObjectDetection]
    ) -> bytes:
        """Draw bounding boxes + labels on frame, return JPEG bytes."""
        import cv2
        import numpy as np

        annotated = np.copy(frame)

        for det in detections:
            color = _label_color(det.label)
            x1, y1 = det.x, det.y
            x2, y2 = det.x + det.width, det.y + det.height

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

            text = f"{det.label} {det.confidence:.0%}"
            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = 0.5
            thickness = 1
            (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)

            # Background rectangle for text
            cv2.rectangle(
                annotated, (x1, y1 - th - baseline - 4), (x1 + tw, y1), color, -1
            )
            cv2.putText(
                annotated, text, (x1, y1 - baseline - 2), font, scale,
                (255, 255, 255), thickness, cv2.LINE_AA,
            )

        ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError("Failed to encode annotated frame as JPEG")
        return buf.tobytes()

    # ------------------------------------------------------------------
    # LLM scene description
    # ------------------------------------------------------------------

    def _describe_with_llm(
        self, jpeg_bytes: bytes, detections: list[ObjectDetection]
    ) -> str:
        """Send frame + detection inventory to Claude Vision for description."""
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic SDK not installed — LLM description disabled")
            return ""

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — LLM description disabled")
            return ""

        b64_image = base64.b64encode(jpeg_bytes).decode("ascii")

        detection_summary = self._format_detection_summary(detections)

        prompt = (
            "Describe this scene from a robot's camera in 2-3 sentences. "
            "Be concise and focus on what's most relevant for a home robot "
            "(people, obstacles, objects it could interact with)."
        )
        if detection_summary:
            prompt += f"\n\nYOLO object detection found: {detection_summary}"

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=self._llm_model,
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64_image,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt,
                            },
                        ],
                    }
                ],
            )
            description = response.content[0].text
            logger.info("LLM scene description: %s", description[:80])
            return description
        except Exception:
            logger.exception("LLM scene description failed")
            return ""

    @staticmethod
    def _format_detection_summary(detections: list[ObjectDetection]) -> str:
        """Format detections into a concise text summary for the LLM prompt."""
        if not detections:
            return ""

        # Group by label and count
        counts: dict[str, int] = {}
        for det in detections:
            counts[det.label] = counts.get(det.label, 0) + 1

        parts = []
        for label, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count == 1:
                parts.append(label)
            else:
                parts.append(f"{count}x {label}")

        return ", ".join(parts)


def _label_color(label: str) -> tuple[int, int, int]:
    """Deterministic BGR color for a detection label."""
    h = hash(label) & 0xFFFFFF
    return (h & 0xFF, (h >> 8) & 0xFF, (h >> 16) & 0xFF)
