"""Unit tests for SceneDescriber — all external deps mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from apps.vector.src.camera.scene_describer import (
    ObjectDetection,
    SceneDescriber,
    SceneResult,
    _label_color,
)
from apps.vector.src.events.event_types import SCENE_DESCRIPTION, SceneDescriptionEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(width: int = 640, height: int = 360) -> np.ndarray:
    """Create a synthetic BGR frame."""
    return np.zeros((height, width, 3), dtype=np.uint8)


def _make_jpeg(frame: np.ndarray | None = None) -> bytes:
    """Encode a frame to JPEG bytes."""
    if frame is None:
        frame = _make_frame()
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


def _make_camera_client(
    frame: np.ndarray | None = None, jpeg: bytes | None = None
) -> MagicMock:
    """Create a mock CameraClient with frame and jpeg data."""
    client = MagicMock()
    if frame is None:
        frame = _make_frame()
    if jpeg is None:
        jpeg = _make_jpeg(frame)
    client.get_latest_frame.return_value = frame
    client.get_latest_jpeg.return_value = jpeg
    return client


def _sample_detections() -> list[ObjectDetection]:
    return [
        ObjectDetection(label="person", confidence=0.92, x=100, y=50, width=80, height=200),
        ObjectDetection(label="chair", confidence=0.78, x=300, y=150, width=60, height=100),
        ObjectDetection(label="person", confidence=0.85, x=400, y=60, width=70, height=190),
    ]


# ---------------------------------------------------------------------------
# ObjectDetection dataclass
# ---------------------------------------------------------------------------

class TestObjectDetection:
    def test_fields(self):
        det = ObjectDetection(label="cup", confidence=0.9, x=10, y=20, width=30, height=40)
        assert det.label == "cup"
        assert det.confidence == 0.9
        assert det.x == 10
        assert det.y == 20
        assert det.width == 30
        assert det.height == 40


# ---------------------------------------------------------------------------
# SceneResult dataclass
# ---------------------------------------------------------------------------

class TestSceneResult:
    def test_defaults(self):
        result = SceneResult(frame_jpeg=b"abc", annotated_jpeg=b"def")
        assert result.detections == []
        assert result.description == ""
        assert result.timestamp == 0.0

    def test_full(self):
        dets = _sample_detections()
        result = SceneResult(
            frame_jpeg=b"raw",
            annotated_jpeg=b"annotated",
            detections=dets,
            description="A room with people.",
            timestamp=1234.5,
        )
        assert len(result.detections) == 3
        assert result.description == "A room with people."


# ---------------------------------------------------------------------------
# SceneDescriber construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_model_dir(self):
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        assert describer._model_dir.name == "models"

    def test_custom_model_dir(self, tmp_path):
        cam = _make_camera_client()
        describer = SceneDescriber(cam, model_dir=tmp_path)
        assert describer._model_dir == tmp_path

    def test_custom_confidence(self):
        cam = _make_camera_client()
        describer = SceneDescriber(cam, yolo_confidence=0.5)
        assert describer._yolo_confidence == 0.5

    def test_custom_llm_model(self):
        cam = _make_camera_client()
        describer = SceneDescriber(cam, llm_model="claude-haiku-4-5-20251001")
        assert describer._llm_model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# YOLO detection (mocked)
# ---------------------------------------------------------------------------

class TestYoloDetection:
    def test_no_ultralytics(self):
        """When ultralytics is not importable, returns empty detections."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)

        with patch.dict("sys.modules", {"ultralytics": None}):
            # Force reload attempt
            describer._yolo_loaded = False
            describer._yolo_model = None
            result = describer.detect_objects(_make_frame())

        assert result == []

    def test_no_model_file(self, tmp_path):
        """When model dir is empty, returns empty detections."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam, model_dir=tmp_path)
        # Mock ultralytics to be importable but no model files exist
        mock_yolo_cls = MagicMock()
        mock_ultralytics = MagicMock()
        mock_ultralytics.YOLO = mock_yolo_cls

        with patch.dict("sys.modules", {"ultralytics": mock_ultralytics}):
            describer._yolo_loaded = False
            describer._yolo_model = None
            result = describer.detect_objects(_make_frame())

        assert result == []
        mock_yolo_cls.assert_not_called()

    def test_yolo_inference_success(self, tmp_path):
        """Successful YOLO inference returns ObjectDetection list."""
        cam = _make_camera_client()
        model_path = tmp_path / "yolo11s.pt"
        model_path.touch()
        describer = SceneDescriber(cam, model_dir=tmp_path)

        # Mock YOLO model and results
        mock_boxes = MagicMock()
        mock_boxes.xyxy = [
            MagicMock(cpu=MagicMock(return_value=MagicMock(numpy=MagicMock(return_value=np.array([100, 50, 180, 250]))))),
        ]
        mock_boxes.cls = [
            MagicMock(cpu=MagicMock(return_value=MagicMock(numpy=MagicMock(return_value=np.float64(0))))),
        ]
        mock_boxes.conf = [
            MagicMock(cpu=MagicMock(return_value=MagicMock(numpy=MagicMock(return_value=np.float64(0.92))))),
        ]
        mock_boxes.__len__ = MagicMock(return_value=1)

        mock_result = MagicMock()
        mock_result.boxes = mock_boxes
        mock_result.names = {0: "person"}

        mock_model = MagicMock(return_value=[mock_result])
        mock_yolo_cls = MagicMock(return_value=mock_model)
        mock_ultralytics = MagicMock()
        mock_ultralytics.YOLO = mock_yolo_cls

        with patch.dict("sys.modules", {"ultralytics": mock_ultralytics}):
            describer._yolo_loaded = False
            describer._yolo_model = None
            result = describer.detect_objects(_make_frame())

        assert len(result) == 1
        assert result[0].label == "person"
        assert result[0].confidence == pytest.approx(0.92)
        assert result[0].x == 100
        assert result[0].y == 50
        assert result[0].width == 80
        assert result[0].height == 200

    def test_yolo_exception_returns_empty(self):
        """YOLO inference exception returns empty list, not crash."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        describer._yolo_loaded = True

        mock_model = MagicMock(side_effect=RuntimeError("GPU OOM"))
        describer._yolo_model = mock_model

        result = describer.detect_objects(_make_frame())
        assert result == []


# ---------------------------------------------------------------------------
# Frame annotation
# ---------------------------------------------------------------------------

class TestAnnotation:
    def test_annotation_returns_jpeg(self):
        """Annotated frame is valid JPEG bytes."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        frame = _make_frame()
        dets = _sample_detections()

        result = describer.get_annotated_frame(frame, dets)

        assert isinstance(result, bytes)
        assert len(result) > 0
        # JPEG magic bytes
        assert result[:2] == b"\xff\xd8"

    def test_annotation_empty_detections(self):
        """No detections still returns valid JPEG."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        frame = _make_frame()

        result = describer.get_annotated_frame(frame, [])

        assert isinstance(result, bytes)
        assert result[:2] == b"\xff\xd8"

    def test_annotation_does_not_modify_input(self):
        """Original frame should not be modified."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        frame = _make_frame()
        original = frame.copy()
        dets = _sample_detections()

        describer.get_annotated_frame(frame, dets)

        np.testing.assert_array_equal(frame, original)


# ---------------------------------------------------------------------------
# LLM description (mocked)
# ---------------------------------------------------------------------------

class TestLlmDescription:
    def test_no_anthropic_sdk(self):
        """Without anthropic SDK, returns empty string."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)

        with patch.dict("sys.modules", {"anthropic": None}):
            result = describer.describe_frame(b"jpeg", [])

        assert result == ""

    def test_no_api_key(self):
        """Without API key, returns empty string."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)
        mock_anthropic = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = describer.describe_frame(b"jpeg", [])

        assert result == ""

    def test_successful_description(self):
        """Successful LLM call returns description text."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)

        mock_content = MagicMock()
        mock_content.text = "A living room with two people sitting on a couch."
        mock_response = MagicMock()
        mock_response.content = [mock_content]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        jpeg = _make_jpeg()
        dets = _sample_detections()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            result = describer.describe_frame(jpeg, dets)

        assert result == "A living room with two people sitting on a couch."

        # Verify the LLM was called with image content
        call_args = mock_client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-sonnet-4-20250514"
        messages = call_args.kwargs["messages"]
        assert len(messages) == 1
        content = messages[0]["content"]
        assert content[0]["type"] == "image"
        assert content[1]["type"] == "text"
        # Detection summary should be included in prompt
        assert "person" in content[1]["text"]

    def test_llm_exception_returns_empty(self):
        """LLM API failure returns empty string, not crash."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("API error")

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            result = describer.describe_frame(_make_jpeg(), [])

        assert result == ""


# ---------------------------------------------------------------------------
# Detection summary formatting
# ---------------------------------------------------------------------------

class TestDetectionSummary:
    def test_empty(self):
        assert SceneDescriber._format_detection_summary([]) == ""

    def test_single(self):
        dets = [ObjectDetection(label="dog", confidence=0.9, x=0, y=0, width=50, height=50)]
        assert SceneDescriber._format_detection_summary(dets) == "dog"

    def test_multiple_same_label(self):
        dets = _sample_detections()  # 2x person, 1x chair
        result = SceneDescriber._format_detection_summary(dets)
        assert "2x person" in result
        assert "chair" in result

    def test_sorted_by_count(self):
        dets = _sample_detections()
        result = SceneDescriber._format_detection_summary(dets)
        # person (2) should come before chair (1)
        assert result.index("person") < result.index("chair")


# ---------------------------------------------------------------------------
# Full pipeline (capture_scene)
# ---------------------------------------------------------------------------

class TestCaptureScene:
    def test_no_frame_raises(self):
        """RuntimeError when camera has no frame."""
        cam = MagicMock()
        cam.get_latest_frame.return_value = None
        cam.get_latest_jpeg.return_value = None
        describer = SceneDescriber(cam)

        with pytest.raises(RuntimeError, match="No frame available"):
            describer.capture_scene()

    def test_full_pipeline_no_yolo_no_llm(self):
        """Pipeline works with YOLO and LLM both unavailable."""
        cam = _make_camera_client()
        describer = SceneDescriber(cam)

        # No YOLO model, no API key
        with patch.dict("sys.modules", {"ultralytics": None}), \
             patch.dict("sys.modules", {"anthropic": None}):
            describer._yolo_loaded = False
            describer._yolo_model = None
            result = describer.capture_scene()

        assert isinstance(result, SceneResult)
        assert result.frame_jpeg == cam.get_latest_jpeg.return_value
        assert isinstance(result.annotated_jpeg, bytes)
        assert result.detections == []
        assert result.description == ""
        assert result.timestamp > 0

    def test_event_emitted(self):
        """SceneDescriptionEvent is emitted on event bus."""
        cam = _make_camera_client()
        bus = NucEventBus()
        received = []
        bus.on(SCENE_DESCRIPTION, lambda data: received.append(data))

        describer = SceneDescriber(cam, event_bus=bus)

        with patch.dict("sys.modules", {"ultralytics": None}), \
             patch.dict("sys.modules", {"anthropic": None}):
            describer._yolo_loaded = False
            describer._yolo_model = None
            describer.capture_scene()

        assert len(received) == 1
        event = received[0]
        assert isinstance(event, SceneDescriptionEvent)
        assert event.detection_count == 0
        assert event.description == ""
        assert event.timestamp > 0


# ---------------------------------------------------------------------------
# Label color helper
# ---------------------------------------------------------------------------

class TestLabelColor:
    def test_deterministic(self):
        c1 = _label_color("person")
        c2 = _label_color("person")
        assert c1 == c2

    def test_different_labels_different_colors(self):
        c1 = _label_color("person")
        c2 = _label_color("chair")
        assert c1 != c2

    def test_returns_bgr_tuple(self):
        color = _label_color("cat")
        assert isinstance(color, tuple)
        assert len(color) == 3
        assert all(0 <= c <= 255 for c in color)
