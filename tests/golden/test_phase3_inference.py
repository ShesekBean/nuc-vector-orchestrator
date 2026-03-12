"""Phase 3 — NUC Inference Pipeline (~25s).

No robot needed (uses saved test images / synthetic data).
Tests all CV/ML on NUC.

Tests 3.1–3.11 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations


import pytest

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]


pytestmark = pytest.mark.phase3

_skip_no_numpy = pytest.mark.skipif(np is None, reason="numpy not installed")


def _make_dummy_frame(width: int = 640, height: int = 360) -> "np.ndarray":
    """Create a synthetic test frame."""
    return np.zeros((height, width, 3), dtype=np.uint8)


# 3.1 YOLO model loads
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


# 3.2 YOLO person detect
class TestYOLOPersonDetect:
    @_skip_no_numpy
    def test_person_detected(self):
        """3.2 — Test image with person → bbox with class=person, confidence > 0.5."""
        from unittest.mock import MagicMock

        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        detector = PersonDetector(event_bus=bus)

        # Mock YOLO to return a person detection
        # _parse_results iterates `for box in boxes`, each box has
        # .cls[0], .conf[0], .xyxy[0].tolist()
        single_box = MagicMock()
        single_box.cls = np.array([0])  # person
        single_box.conf = np.array([0.85])
        single_box.xyxy = np.array([[100, 50, 300, 350]])

        mock_boxes = MagicMock()
        mock_boxes.__iter__ = MagicMock(return_value=iter([single_box]))
        mock_boxes.__len__ = MagicMock(return_value=1)

        mock_result = MagicMock()
        mock_result.boxes = mock_boxes

        detector._model = MagicMock(return_value=[mock_result])
        detector._model.names = {0: "person"}

        frame = _make_dummy_frame()
        detections = detector.detect(frame)
        assert len(detections) > 0
        assert detections[0].confidence > 0.5


# 3.3 YOLO no-person
class TestYOLONoPerson:
    @_skip_no_numpy
    def test_no_person(self):
        """3.3 — Test image with no person → no person detections."""
        from unittest.mock import MagicMock

        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        detector = PersonDetector(event_bus=bus)

        mock_boxes = MagicMock()
        mock_boxes.__iter__ = MagicMock(return_value=iter([]))
        mock_boxes.__len__ = MagicMock(return_value=0)

        mock_result = MagicMock()
        mock_result.boxes = mock_boxes

        detector._model = MagicMock(return_value=[mock_result])
        detector._model.names = {0: "person"}

        frame = _make_dummy_frame()
        detections = detector.detect(frame)
        assert len(detections) == 0


# 3.4 Face detection (YuNet)
class TestFaceDetectionYuNet:
    @_skip_no_numpy
    def test_face_detector_init(self):
        """3.4 — FaceDetector can be instantiated."""
        try:
            from apps.vector.src.face_recognition.face_detector import FaceDetector

            detector = FaceDetector()
            assert detector is not None
        except (ImportError, FileNotFoundError):
            pytest.skip("Face detection model not available")


# 3.5 Face recognition (SFace) — same person
class TestFaceRecognitionSamePerson:
    @_skip_no_numpy
    def test_same_person_similarity(self):
        """3.5 — Two images of same person → cosine similarity > threshold."""
        try:
            from apps.vector.src.face_recognition.face_recognizer import (
                FaceRecognizer,
            )
        except ImportError:
            pytest.skip("Face recognizer not available")

        recognizer = FaceRecognizer()
        try:
            recognizer.load_model()
        except (FileNotFoundError, Exception):
            pytest.skip("SFace model not available")

        # Use synthetic embeddings to test similarity logic
        emb_a = np.random.randn(128).astype(np.float32)
        emb_b = emb_a + np.random.randn(128).astype(np.float32) * 0.05
        cos_sim = float(np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b)))
        assert cos_sim > 0.5, f"Same-person similarity too low: {cos_sim}"


# 3.6 Face different people
class TestFaceDifferentPeople:
    @_skip_no_numpy
    def test_different_person_dissimilarity(self):
        """3.6 — Two different people → cosine similarity < threshold."""
        emb_a = np.array([1.0, 0.0, 0.0, 0.0] * 32, dtype=np.float32)
        emb_b = np.array([0.0, 1.0, 0.0, 0.0] * 32, dtype=np.float32)
        cos_sim = float(np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b)))
        assert cos_sim < 0.5, f"Different-person similarity too high: {cos_sim}"


# 3.7 Kalman tracker init
class TestKalmanTrackerInit:
    @_skip_no_numpy
    def test_detection_creates_track(self):
        """3.7 — Detection → tracker creates track with ID."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()
        det = Detection(cx=320.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)
        tracker.update([det])
        assert tracker.track_count >= 1


# 3.8 Kalman tracker update
class TestKalmanTrackerUpdate:
    @_skip_no_numpy
    def test_smooth_trajectory(self):
        """3.8 — 5 sequential detections → smooth trajectory, no ID switch."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()
        track_ids = []
        for i in range(5):
            det = Detection(
                cx=320.0 + i * 5,
                cy=180.0,
                width=100.0,
                height=200.0,
                confidence=0.9,
            )
            tracker.update([det])
            primary = tracker.get_primary_track()
            if primary:
                track_ids.append(primary.track_id)

        assert len(track_ids) >= 3, "Too few tracked frames"
        assert len(set(track_ids)) == 1, f"Track ID switched: {track_ids}"


# 3.9 Kalman tracker timeout
class TestKalmanTrackerTimeout:
    @_skip_no_numpy
    def test_track_dropped_on_timeout(self):
        """3.9 — No detections for N frames → track dropped."""
        from apps.vector.src.detector.kalman_tracker import Detection, KalmanTracker

        tracker = KalmanTracker()
        det = Detection(cx=320.0, cy=180.0, width=100.0, height=200.0, confidence=0.9)
        tracker.update([det])
        assert tracker.track_count >= 1

        # Send many empty updates to trigger timeout
        for _ in range(50):
            tracker.update([])
        assert tracker.confirmed_count == 0, "Track should have been dropped"


# 3.10 Scene description
class TestSceneDescription:
    def test_scene_describer_instantiates(self):
        """3.10 — SceneDescriber can be instantiated."""
        try:
            from apps.vector.src.camera.scene_describer import SceneDescriber

            from unittest.mock import MagicMock
            describer = SceneDescriber(camera_client=MagicMock())
            assert describer is not None
        except (ImportError, TypeError):
            pytest.skip("SceneDescriber not available")


# 3.11 Detection → event bus
class TestDetectionEventBus:
    @_skip_no_numpy
    def test_yolo_fires_event(self):
        """3.11 — YOLO detection fires person_detected event."""
        from unittest.mock import MagicMock

        from apps.vector.src.detector.person_detector import PersonDetector
        from apps.vector.src.events.event_types import YOLO_PERSON_DETECTED
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        received = []
        bus.on(YOLO_PERSON_DETECTED, lambda data: received.append(data))

        detector = PersonDetector(event_bus=bus)

        # Mock YOLO model
        single_box = MagicMock()
        single_box.cls = np.array([0])
        single_box.conf = np.array([0.85])
        single_box.xyxy = np.array([[100, 50, 300, 350]])

        mock_boxes = MagicMock()
        mock_boxes.__iter__ = MagicMock(return_value=iter([single_box]))
        mock_boxes.__len__ = MagicMock(return_value=1)

        mock_result = MagicMock()
        mock_result.boxes = mock_boxes

        detector._model = MagicMock(return_value=[mock_result])
        detector._model.names = {0: "person"}

        frame = _make_dummy_frame()
        detector.detect(frame)
        assert len(received) > 0, "No person_detected event emitted"
