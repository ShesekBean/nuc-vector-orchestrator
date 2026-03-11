"""Unit tests for YuNet face detector.

Tests use mocked cv2.FaceDetectorYN since CI environments may not have
the full OpenCV contrib with face detection support. Tests verify:
- Lazy model loading
- Detection parsing (bbox, landmarks, confidence)
- Confidence threshold filtering
- Empty detection handling
- Confidence sorting
- Missing model error
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from apps.vector.src.face_recognition.face_detector import (
    FaceDetection,
    FaceDetector,
)


def _make_yunet_output(faces: list[dict]) -> np.ndarray:
    """Build a YuNet-format output array from simplified face dicts.

    Each dict: {x, y, w, h, score} with optional landmarks.
    """
    rows = []
    for f in faces:
        cx = f["x"] + f["w"] / 2
        cy = f["y"] + f["h"] / 2
        row = [
            f["x"], f["y"], f["w"], f["h"],
            cx - 10, cy - 10,  # right eye
            cx + 10, cy - 10,  # left eye
            cx, cy,            # nose
            cx - 5, cy + 10,   # right mouth
            cx + 5, cy + 10,   # left mouth
            f["score"],
        ]
        rows.append(row)
    return np.array(rows, dtype=np.float32)


@pytest.fixture
def mock_cv2():
    """Inject a mock cv2 module that the lazy import will pick up."""
    mock_detector_instance = MagicMock()
    mock_detector_instance.detect.return_value = (1, None)

    mock_cv2_mod = MagicMock()
    mock_cv2_mod.FaceDetectorYN.create.return_value = mock_detector_instance
    mock_cv2_mod.dnn.DNN_BACKEND_INFERENCE_ENGINE = 1
    mock_cv2_mod.dnn.DNN_TARGET_CPU = 0

    with patch.dict(sys.modules, {"cv2": mock_cv2_mod}):
        yield mock_cv2_mod, mock_detector_instance


class TestFaceDetection:
    def test_dataclass_frozen(self):
        det = FaceDetection(x=10, y=20, width=50, height=60, confidence=0.9)
        with pytest.raises(AttributeError):
            det.x = 99

    def test_default_landmarks_empty(self):
        det = FaceDetection(x=0, y=0, width=10, height=10, confidence=0.5)
        assert det.landmarks == ()

    def test_with_landmarks(self):
        lm = ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0), (9.0, 10.0))
        det = FaceDetection(x=0, y=0, width=10, height=10, confidence=0.5, landmarks=lm)
        assert len(det.landmarks) == 5
        assert det.landmarks[2] == (5.0, 6.0)


class TestFaceDetectorInit:
    def test_not_loaded_initially(self):
        detector = FaceDetector(model_path="/fake/model.onnx")
        assert not detector.is_loaded

    def test_default_confidence_threshold(self):
        detector = FaceDetector(model_path="/fake/model.onnx")
        assert detector.confidence_threshold == 0.5


class TestFaceDetectorDetect:
    def test_lazy_load_on_first_detect(self, mock_cv2, tmp_path):
        cv2_mock, mock_det = mock_cv2
        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake model")

        detector = FaceDetector(model_path=str(model_file))
        assert not detector.is_loaded

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        detector.detect(frame)
        assert detector.is_loaded
        cv2_mock.FaceDetectorYN.create.assert_called_once()

    def test_no_detections(self, mock_cv2, tmp_path):
        _, mock_det = mock_cv2
        mock_det.detect.return_value = (0, None)

        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake")
        detector = FaceDetector(model_path=str(model_file))

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        result = detector.detect(frame)
        assert result == []

    def test_single_face_detection(self, mock_cv2, tmp_path):
        _, mock_det = mock_cv2
        faces = _make_yunet_output([
            {"x": 100, "y": 50, "w": 80, "h": 90, "score": 0.92},
        ])
        mock_det.detect.return_value = (1, faces)

        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake")
        detector = FaceDetector(model_path=str(model_file))

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        result = detector.detect(frame)

        assert len(result) == 1
        assert result[0].x == pytest.approx(100.0)
        assert result[0].y == pytest.approx(50.0)
        assert result[0].width == pytest.approx(80.0)
        assert result[0].height == pytest.approx(90.0)
        assert result[0].confidence == pytest.approx(0.92, abs=0.01)
        assert len(result[0].landmarks) == 5

    def test_multiple_faces_sorted_by_confidence(self, mock_cv2, tmp_path):
        _, mock_det = mock_cv2
        faces = _make_yunet_output([
            {"x": 100, "y": 50, "w": 80, "h": 90, "score": 0.60},
            {"x": 300, "y": 50, "w": 80, "h": 90, "score": 0.95},
            {"x": 200, "y": 50, "w": 80, "h": 90, "score": 0.78},
        ])
        mock_det.detect.return_value = (3, faces)

        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake")
        detector = FaceDetector(model_path=str(model_file))

        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        result = detector.detect(frame)

        assert len(result) == 3
        assert result[0].confidence > result[1].confidence > result[2].confidence
        assert result[0].x == pytest.approx(300.0)

    def test_sets_input_size_per_frame(self, mock_cv2, tmp_path):
        _, mock_det = mock_cv2

        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake")
        detector = FaceDetector(model_path=str(model_file))

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detector.detect(frame)
        mock_det.setInputSize.assert_called_with((640, 480))

    def test_missing_model_raises(self, mock_cv2):
        detector = FaceDetector(model_path="/nonexistent/model.onnx")
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        with pytest.raises(FileNotFoundError, match="YuNet model not found"):
            detector.detect(frame)


class TestFaceDetectorThreshold:
    def test_set_threshold_updates_detector(self, mock_cv2, tmp_path):
        _, mock_det = mock_cv2
        mock_det.detect.return_value = (0, None)

        model_file = tmp_path / "yunet.onnx"
        model_file.write_bytes(b"fake")
        detector = FaceDetector(model_path=str(model_file))

        # Trigger load
        detector.detect(np.zeros((360, 640, 3), dtype=np.uint8))

        detector.confidence_threshold = 0.7
        assert detector.confidence_threshold == 0.7
        mock_det.setScoreThreshold.assert_called_with(0.7)

    def test_set_threshold_before_load(self):
        detector = FaceDetector(model_path="/fake/model.onnx")
        detector.confidence_threshold = 0.3
        assert detector.confidence_threshold == 0.3
