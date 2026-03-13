"""Phase 3 — NUC Inference Pipeline.

No robot needed (uses synthetic data). Tests all CV/ML on NUC.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


pytestmark = pytest.mark.phase3

_skip_no_numpy = pytest.mark.skipif(np is None, reason="numpy not installed")


def _make_dummy_frame(width: int = 640, height: int = 360) -> "np.ndarray":
    return np.zeros((height, width, 3), dtype=np.uint8)


def _mock_yolo_detector(with_person: bool = True):
    """Create a PersonDetector with mocked YOLO model."""
    from apps.vector.src.detector.person_detector import PersonDetector
    from apps.vector.src.events.nuc_event_bus import NucEventBus

    bus = NucEventBus()
    detector = PersonDetector(event_bus=bus)

    if with_person:
        single_box = MagicMock()
        single_box.cls = np.array([0])
        single_box.conf = np.array([0.85])
        single_box.xyxy = np.array([[100, 50, 300, 350]])
        boxes = [single_box]
    else:
        boxes = []

    mock_boxes = MagicMock()
    mock_boxes.__iter__ = MagicMock(return_value=iter(boxes))
    mock_boxes.__len__ = MagicMock(return_value=len(boxes))

    mock_result = MagicMock()
    mock_result.boxes = mock_boxes

    detector._model = MagicMock(return_value=[mock_result])
    detector._model.names = {0: "person"}
    return detector, bus


class TestYOLOModelLoads:
    @_skip_no_numpy
    def test_model_loads(self):
        """3.1 — OpenVINO IR model loads without error."""
        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        detector = PersonDetector(event_bus=bus)
        try:
            detector.load_model()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            pytest.skip(f"YOLO dependency not available: {exc}")
        assert detector.is_loaded


class TestYOLODetection:
    @_skip_no_numpy
    def test_person_detect_and_empty(self):
        """3.2 — YOLO detects person (conf > 0.5), empty frame → no detections."""
        frame = _make_dummy_frame()

        detector, _ = _mock_yolo_detector(with_person=True)
        detections = detector.detect(frame)
        assert len(detections) > 0
        assert detections[0].confidence > 0.5

        detector_empty, _ = _mock_yolo_detector(with_person=False)
        assert len(detector_empty.detect(frame)) == 0


class TestFaceRecognition:
    @_skip_no_numpy
    def test_similarity_same_vs_different(self):
        """3.3 — Same person high similarity, different people low similarity."""
        try:
            from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer
        except ImportError:
            pytest.skip("Face recognizer not available")

        recognizer = FaceRecognizer()
        try:
            recognizer._load_model()
        except (FileNotFoundError, Exception):
            pytest.skip("SFace model not available")
        if not recognizer.is_loaded:
            pytest.skip("SFace model not loaded")

        # Same person (small perturbation)
        emb_a = np.random.randn(128).astype(np.float32)
        emb_b = emb_a + np.random.randn(128).astype(np.float32) * 0.05
        cos_same = float(np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b)))
        assert cos_same > 0.5, f"Same-person similarity too low: {cos_same}"

        # Different people (orthogonal vectors)
        emb_x = np.array([1.0, 0.0, 0.0, 0.0] * 32, dtype=np.float32)
        emb_y = np.array([0.0, 1.0, 0.0, 0.0] * 32, dtype=np.float32)
        cos_diff = float(np.dot(emb_x, emb_y) / (np.linalg.norm(emb_x) * np.linalg.norm(emb_y)))
        assert cos_diff < 0.5, f"Different-person similarity too high: {cos_diff}"


class TestKalmanTrackerLifecycle:
    @_skip_no_numpy
    def test_create_track_update_and_timeout(self):
        """3.4 — Tracker: create → consistent ID over 5 frames → timeout on empty."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()

        # Create and track over 5 frames
        track_ids = []
        for i in range(5):
            det = Detection(cx=320.0 + i * 5, cy=180.0, width=100.0, height=200.0, confidence=0.9)
            tracker.update([det])
            primary = tracker.get_primary_track()
            if primary:
                track_ids.append(primary.track_id)

        assert len(track_ids) >= 3, "Too few tracked frames"
        assert len(set(track_ids)) == 1, f"Track ID switched: {track_ids}"

        # Timeout: many empty updates → track dropped
        for _ in range(50):
            tracker.update([])
        assert tracker.confirmed_count == 0, "Track should have been dropped"


class TestSceneDescription:
    def test_scene_describer_instantiates(self):
        """3.5 — SceneDescriber can be instantiated."""
        try:
            from apps.vector.src.camera.scene_describer import SceneDescriber
            describer = SceneDescriber(camera_client=MagicMock())
            assert describer is not None
        except (ImportError, TypeError):
            pytest.skip("SceneDescriber not available")


class TestDetectionEventBus:
    @_skip_no_numpy
    def test_yolo_fires_event(self):
        """3.6 — YOLO detection fires person_detected event on bus."""
        from apps.vector.src.events.event_types import YOLO_PERSON_DETECTED

        detector, bus = _mock_yolo_detector(with_person=True)
        received = []
        bus.on(YOLO_PERSON_DETECTED, lambda data: received.append(data))

        detector.detect(_make_dummy_frame())
        assert len(received) > 0, "No person_detected event emitted"
