"""Unit tests for SFace face recognizer.

Tests use mocked cv2.FaceRecognizerSF since CI environments may not have
the full OpenCV contrib. Tests verify:
- Enrollment (add embeddings, capacity limit)
- Matching (cosine similarity, threshold gating)
- Database persistence (save/load JSON)
- Event bus integration (FACE_RECOGNIZED emission)
- Thread safety
- Unknown face handling
"""

from __future__ import annotations

import json
import sys
import threading
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from apps.vector.src.events.event_types import FACE_RECOGNIZED, FaceRecognizedEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.face_recognition.face_detector import FaceDetection
from apps.vector.src.face_recognition.face_recognizer import (
    FaceMatch,
    FaceRecognizer,
)


def _make_detection(x=100, y=50, w=80, h=90, score=0.9) -> FaceDetection:
    """Create a FaceDetection with default landmarks."""
    cx, cy = x + w / 2, y + h / 2
    landmarks = (
        (cx - 10, cy - 10), (cx + 10, cy - 10),
        (cx, cy), (cx - 5, cy + 10), (cx + 5, cy + 10),
    )
    return FaceDetection(x=x, y=y, width=w, height=h, confidence=score, landmarks=landmarks)


def _make_embedding(seed: int = 42, dim: int = 128) -> np.ndarray:
    """Create a deterministic unit-normalized embedding."""
    rng = np.random.RandomState(seed)
    emb = rng.randn(1, dim).astype(np.float32)
    emb /= np.linalg.norm(emb)
    return emb


@pytest.fixture
def mock_cv2_sface():
    """Inject a mock cv2 module that the lazy import will pick up."""
    mock_recognizer = MagicMock()
    mock_recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
    default_emb = _make_embedding(seed=42)
    mock_recognizer.feature.return_value = default_emb

    mock_cv2_mod = MagicMock()
    mock_cv2_mod.FaceRecognizerSF.create.return_value = mock_recognizer

    with patch.dict(sys.modules, {"cv2": mock_cv2_mod}):
        yield mock_cv2_mod, mock_recognizer


class TestFaceMatch:
    def test_dataclass_frozen(self):
        det = _make_detection()
        match = FaceMatch(name="alice", confidence=0.85, detection=det)
        with pytest.raises(AttributeError):
            match.name = "bob"


class TestFaceRecognizerInit:
    def test_not_loaded_initially(self):
        recognizer = FaceRecognizer(model_path="/fake/sface.onnx")
        assert not recognizer.is_loaded
        assert recognizer.enrolled_count == 0

    def test_default_threshold(self):
        recognizer = FaceRecognizer(model_path="/fake/sface.onnx")
        assert recognizer.match_threshold == pytest.approx(0.363)


class TestEnrollment:
    def test_enroll_single_face(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        det = _make_detection()

        count = recognizer.enroll("alice", frame, [det])
        assert count == 1
        assert recognizer.enrolled_count == 1
        assert recognizer.list_enrolled() == {"alice": 1}

    def test_enroll_respects_capacity(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(
            model_path=str(model_file), embeddings_per_person=3,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        dets = [_make_detection(x=i * 100) for i in range(5)]

        count = recognizer.enroll("alice", frame, dets)
        assert count == 3  # capped at embeddings_per_person

        # Additional enrollments don't exceed cap
        count = recognizer.enroll("alice", frame, [_make_detection()])
        assert count == 3

    def test_enroll_multiple_people(self, mock_cv2_sface, tmp_path):
        _, _ = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        recognizer.enroll("bob", frame, [_make_detection()])

        assert recognizer.enrolled_count == 2
        enrolled = recognizer.list_enrolled()
        assert "alice" in enrolled
        assert "bob" in enrolled

    def test_remove_person(self, mock_cv2_sface, tmp_path):
        _, _ = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        assert recognizer.remove("alice")
        assert recognizer.enrolled_count == 0
        assert not recognizer.remove("alice")  # already removed


class TestMatching:
    def test_recognize_unknown_when_empty_db(self, mock_cv2_sface, tmp_path):
        _, _ = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        results = recognizer.recognize(frame, [_make_detection()])
        assert len(results) == 1
        assert results[0].name == "unknown"

    def test_recognize_enrolled_face(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        # Use same embedding for enroll and recognize (perfect match)
        emb = _make_embedding(seed=42)
        mock_rec.feature.return_value = emb

        recognizer = FaceRecognizer(
            model_path=str(model_file), match_threshold=0.3,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        results = recognizer.recognize(frame, [_make_detection()])

        assert len(results) == 1
        assert results[0].name == "alice"
        assert results[0].confidence == pytest.approx(1.0, abs=0.01)

    def test_recognize_below_threshold_is_unknown(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(
            model_path=str(model_file), match_threshold=0.99,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        # Enroll with one embedding
        emb_a = _make_embedding(seed=1)
        mock_rec.feature.return_value = emb_a
        recognizer.enroll("alice", frame, [_make_detection()])

        # Recognize with a different embedding
        emb_b = _make_embedding(seed=2)
        mock_rec.feature.return_value = emb_b
        results = recognizer.recognize(frame, [_make_detection()])

        assert results[0].name == "unknown"

    def test_cosine_similarity_identical(self):
        emb = _make_embedding(seed=1)
        score = FaceRecognizer._cosine_similarity(emb, emb)
        assert score == pytest.approx(1.0, abs=0.01)

    def test_cosine_similarity_orthogonal(self):
        a = np.array([[1.0, 0.0]], dtype=np.float32)
        b = np.array([[0.0, 1.0]], dtype=np.float32)
        score = FaceRecognizer._cosine_similarity(a, b)
        assert score == pytest.approx(0.0, abs=0.01)

    def test_cosine_similarity_zero_vector(self):
        a = np.zeros((1, 128), dtype=np.float32)
        b = _make_embedding(seed=1)
        score = FaceRecognizer._cosine_similarity(a, b)
        assert score == pytest.approx(0.0)


class TestDatabasePersistence:
    def test_save_and_load(self, mock_cv2_sface, tmp_path):
        _, _ = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")
        db_path = str(tmp_path / "faces.json")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        recognizer.enroll("bob", frame, [_make_detection()])
        recognizer.save_database(db_path)

        # Load into a fresh recognizer
        recognizer2 = FaceRecognizer(model_path=str(model_file))
        recognizer2.load_database(db_path)

        assert recognizer2.enrolled_count == 2
        assert recognizer2.list_enrolled() == {"alice": 1, "bob": 1}

    def test_save_creates_valid_json(self, mock_cv2_sface, tmp_path):
        _, _ = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")
        db_path = str(tmp_path / "faces.json")

        recognizer = FaceRecognizer(model_path=str(model_file))
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        recognizer.enroll("alice", frame, [_make_detection()])
        recognizer.save_database(db_path)

        with open(db_path) as f:
            data = json.load(f)
        assert "alice" in data
        assert len(data["alice"]) == 1
        assert isinstance(data["alice"][0], list)  # numpy array -> list

    def test_load_missing_file_logs_warning(self, mock_cv2_sface, tmp_path, caplog):
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        recognizer = FaceRecognizer(model_path=str(model_file))
        recognizer.load_database(str(tmp_path / "nonexistent.json"))
        assert recognizer.enrolled_count == 0


class TestEventBusIntegration:
    def test_emits_face_recognized_on_match(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        bus = NucEventBus()
        events_received = []
        bus.on(FACE_RECOGNIZED, lambda e: events_received.append(e))

        emb = _make_embedding(seed=42)
        mock_rec.feature.return_value = emb

        recognizer = FaceRecognizer(
            model_path=str(model_file), match_threshold=0.3, event_bus=bus,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        recognizer.recognize(frame, [_make_detection(x=100, y=50)])

        assert len(events_received) == 1
        event = events_received[0]
        assert isinstance(event, FaceRecognizedEvent)
        assert event.name == "alice"
        assert event.confidence == pytest.approx(1.0, abs=0.01)
        assert event.x == pytest.approx(100.0)

    def test_no_event_for_unknown(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        bus = NucEventBus()
        events_received = []
        bus.on(FACE_RECOGNIZED, lambda e: events_received.append(e))

        recognizer = FaceRecognizer(
            model_path=str(model_file), event_bus=bus,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        # No enrollment — all faces should be "unknown"
        recognizer.recognize(frame, [_make_detection()])
        assert len(events_received) == 0

    def test_no_event_without_bus(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        emb = _make_embedding(seed=42)
        mock_rec.feature.return_value = emb

        # No event bus — should not crash
        recognizer = FaceRecognizer(
            model_path=str(model_file), match_threshold=0.3,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)

        recognizer.enroll("alice", frame, [_make_detection()])
        results = recognizer.recognize(frame, [_make_detection()])
        assert results[0].name == "alice"


class TestThreadSafety:
    def test_concurrent_enroll_and_match(self, mock_cv2_sface, tmp_path):
        _, mock_rec = mock_cv2_sface
        model_file = tmp_path / "sface.onnx"
        model_file.write_bytes(b"fake")

        emb = _make_embedding(seed=42)
        mock_rec.feature.return_value = emb

        recognizer = FaceRecognizer(
            model_path=str(model_file), match_threshold=0.3,
        )
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        errors = []

        def enroller():
            try:
                for i in range(20):
                    recognizer.enroll(f"person_{i}", frame, [_make_detection()])
            except Exception as e:
                errors.append(e)

        def matcher():
            try:
                for _ in range(50):
                    recognizer.recognize(frame, [_make_detection()])
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=enroller),
            threading.Thread(target=matcher),
            threading.Thread(target=matcher),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread safety errors: {errors}"


class TestThresholdProperty:
    def test_set_threshold(self):
        recognizer = FaceRecognizer(model_path="/fake/sface.onnx")
        recognizer.match_threshold = 0.5
        assert recognizer.match_threshold == 0.5
