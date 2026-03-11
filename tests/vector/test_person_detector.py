"""Unit tests for PersonDetector.

Uses mocked ultralytics so tests run in CI without the actual model.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers — fake ultralytics results
# ---------------------------------------------------------------------------

def _make_box(cls_id: int, conf: float, xyxy: list[float]) -> MagicMock:
    """Create a fake ultralytics box object."""
    box = MagicMock()
    box.cls = [cls_id]
    box.conf = [conf]
    box.xyxy = [MagicMock(tolist=MagicMock(return_value=xyxy))]
    return box


def _make_results(boxes: list | None = None) -> list[MagicMock]:
    """Wrap boxes into a fake ultralytics Results list."""
    result = MagicMock()
    if boxes is None:
        result.boxes = None
    else:
        # Wrap list in a MagicMock so __len__/__iter__ can be set
        mock_boxes = MagicMock()
        mock_boxes.__len__ = MagicMock(return_value=len(boxes))
        mock_boxes.__iter__ = MagicMock(return_value=iter(boxes))
        result.boxes = mock_boxes
    return [result]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def dummy_frame():
    """640x360 BGR numpy array (black)."""
    return np.zeros((360, 640, 3), dtype=np.uint8)


@pytest.fixture()
def mock_yolo_class():
    """Patch ultralytics.YOLO and return the mock class."""
    with patch.dict("sys.modules", {"ultralytics": types.ModuleType("ultralytics")}):
        import sys
        mock_yolo = MagicMock()
        sys.modules["ultralytics"].YOLO = mock_yolo
        yield mock_yolo


@pytest.fixture()
def event_bus():
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    return NucEventBus()


# ---------------------------------------------------------------------------
# Tests — construction & loading
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_values(self):
        from apps.vector.src.detector.person_detector import PersonDetector
        det = PersonDetector.__new__(PersonDetector)
        det.__init__()
        assert det.confidence_threshold == 0.25
        assert det.frame_count == 0
        assert det.fps == 0.0
        assert not det.is_loaded

    def test_custom_threshold(self):
        from apps.vector.src.detector.person_detector import PersonDetector
        det = PersonDetector.__new__(PersonDetector)
        det.__init__(confidence_threshold=0.5)
        assert det.confidence_threshold == 0.5

    def test_threshold_setter_clamps(self):
        from apps.vector.src.detector.person_detector import PersonDetector
        det = PersonDetector.__new__(PersonDetector)
        det.__init__()
        det.confidence_threshold = 1.5
        assert det.confidence_threshold == 1.0
        det.confidence_threshold = -0.1
        assert det.confidence_threshold == 0.0

    def test_model_not_found_raises(self, tmp_path, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector
        det = PersonDetector(model_path=str(tmp_path / "nonexistent"))
        with pytest.raises(FileNotFoundError, match="YOLO model not found"):
            det.load_model()


# ---------------------------------------------------------------------------
# Tests — detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_single_person_detection(self, dummy_frame, mock_yolo_class, event_bus):
        from apps.vector.src.detector.person_detector import PersonDetector

        # Setup mock model to return one person box
        mock_model_instance = MagicMock()
        person_box = _make_box(cls_id=0, conf=0.85, xyxy=[100, 50, 200, 300])
        mock_model_instance.return_value = _make_results([person_box])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp", event_bus=event_bus)
        det._model = mock_model_instance  # skip load_model

        results = det.detect(dummy_frame)

        assert len(results) == 1
        assert results[0].cx == pytest.approx(150.0)
        assert results[0].cy == pytest.approx(175.0)
        assert results[0].width == pytest.approx(100.0)
        assert results[0].height == pytest.approx(250.0)
        assert results[0].confidence == pytest.approx(0.85)

    def test_filters_non_person_classes(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        # Class 0 = person, class 16 = dog
        person_box = _make_box(cls_id=0, conf=0.9, xyxy=[10, 20, 110, 220])
        dog_box = _make_box(cls_id=16, conf=0.8, xyxy=[300, 100, 400, 250])
        mock_model_instance.return_value = _make_results([person_box, dog_box])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        results = det.detect(dummy_frame)
        assert len(results) == 1
        assert results[0].confidence == pytest.approx(0.9)

    def test_empty_results(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        mock_model_instance.return_value = _make_results([])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        results = det.detect(dummy_frame)
        assert results == []

    def test_none_boxes(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        mock_model_instance.return_value = _make_results(None)
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        results = det.detect(dummy_frame)
        assert results == []

    def test_multiple_persons(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        boxes = [
            _make_box(cls_id=0, conf=0.9, xyxy=[0, 0, 100, 200]),
            _make_box(cls_id=0, conf=0.7, xyxy=[300, 50, 450, 350]),
            _make_box(cls_id=0, conf=0.3, xyxy=[500, 100, 600, 300]),
        ]
        mock_model_instance.return_value = _make_results(boxes)
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        results = det.detect(dummy_frame)
        assert len(results) == 3
        confidences = [r.confidence for r in results]
        assert confidences == [pytest.approx(0.9), pytest.approx(0.7), pytest.approx(0.3)]


# ---------------------------------------------------------------------------
# Tests — event bus integration
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_emits_yolo_person_detected(self, dummy_frame, mock_yolo_class, event_bus):
        from apps.vector.src.events.event_types import YOLO_PERSON_DETECTED, YoloPersonDetectedEvent
        from apps.vector.src.detector.person_detector import PersonDetector

        received = []
        event_bus.on(YOLO_PERSON_DETECTED, received.append)

        mock_model_instance = MagicMock()
        person_box = _make_box(cls_id=0, conf=0.88, xyxy=[50, 60, 150, 260])
        mock_model_instance.return_value = _make_results([person_box])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp", event_bus=event_bus)
        det._model = mock_model_instance

        det.detect(dummy_frame)

        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, YoloPersonDetectedEvent)
        assert evt.x == pytest.approx(100.0)
        assert evt.y == pytest.approx(160.0)
        assert evt.width == pytest.approx(100.0)
        assert evt.height == pytest.approx(200.0)
        assert evt.confidence == pytest.approx(0.88)
        assert evt.frame_width == 640
        assert evt.frame_height == 360

    def test_no_event_without_bus(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        person_box = _make_box(cls_id=0, conf=0.9, xyxy=[10, 20, 110, 220])
        mock_model_instance.return_value = _make_results([person_box])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")  # no event_bus
        det._model = mock_model_instance

        # Should not raise
        results = det.detect(dummy_frame)
        assert len(results) == 1

    def test_multiple_detections_emit_multiple_events(self, dummy_frame, mock_yolo_class, event_bus):
        from apps.vector.src.events.event_types import YOLO_PERSON_DETECTED
        from apps.vector.src.detector.person_detector import PersonDetector

        received = []
        event_bus.on(YOLO_PERSON_DETECTED, received.append)

        mock_model_instance = MagicMock()
        boxes = [
            _make_box(cls_id=0, conf=0.9, xyxy=[0, 0, 100, 200]),
            _make_box(cls_id=0, conf=0.8, xyxy=[300, 50, 450, 350]),
        ]
        mock_model_instance.return_value = _make_results(boxes)
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp", event_bus=event_bus)
        det._model = mock_model_instance

        det.detect(dummy_frame)
        assert len(received) == 2


# ---------------------------------------------------------------------------
# Tests — metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_frame_count_increments(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        mock_model_instance.return_value = _make_results([])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        assert det.frame_count == 0
        det.detect(dummy_frame)
        assert det.frame_count == 1
        det.detect(dummy_frame)
        det.detect(dummy_frame)
        assert det.frame_count == 3

    def test_avg_inference_ms(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector

        mock_model_instance = MagicMock()
        mock_model_instance.return_value = _make_results([])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        det.detect(dummy_frame)
        # Just verify it returns a positive number
        assert det.avg_inference_ms > 0
        assert det.fps > 0

    def test_fps_zero_when_no_frames(self):
        from apps.vector.src.detector.person_detector import PersonDetector
        det = PersonDetector.__new__(PersonDetector)
        det.__init__()
        assert det.fps == 0.0
        assert det.avg_inference_ms == 0.0


# ---------------------------------------------------------------------------
# Tests — KalmanTracker integration (data format compatibility)
# ---------------------------------------------------------------------------

class TestKalmanTrackerCompat:
    def test_detection_feeds_into_tracker(self, dummy_frame, mock_yolo_class):
        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.detector.kalman_tracker import KalmanTracker

        mock_model_instance = MagicMock()
        person_box = _make_box(cls_id=0, conf=0.9, xyxy=[100, 50, 200, 300])
        mock_model_instance.return_value = _make_results([person_box])
        mock_yolo_class.return_value = mock_model_instance

        det = PersonDetector(model_path="/tmp")
        det._model = mock_model_instance

        detections = det.detect(dummy_frame)

        # Feed directly into KalmanTracker — must not raise
        tracker = KalmanTracker()
        confirmed = tracker.update(detections)
        # First frame won't be confirmed (needs min_hits=3)
        assert isinstance(confirmed, list)

        # After 3 updates the track should be confirmed
        for _ in range(2):
            tracker.update(detections)
        confirmed = tracker.update(detections)
        assert len(confirmed) >= 1
        assert confirmed[0].cx == pytest.approx(150.0, abs=5)
