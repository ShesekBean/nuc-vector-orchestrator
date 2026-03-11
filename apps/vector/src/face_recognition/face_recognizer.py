"""SFace face recognizer with enrollment database and event bus integration.

Wraps cv2.FaceRecognizerSF for face embedding extraction and cosine
similarity matching. Maintains an in-memory database of enrolled face
embeddings that can be persisted to disk as JSON.

R3 reference: 5 embeddings per enrollment, threshold 0.363 (cosine).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass

import numpy as np

from apps.vector.src.events.event_types import FACE_RECOGNIZED, FaceRecognizedEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.face_recognition.face_detector import FaceDetection

logger = logging.getLogger(__name__)

# Default model path
_DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "models",
)
_SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"

# Default database path
_DEFAULT_DB_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "data",
)
_DEFAULT_DB_FILENAME = "face_database.json"


@dataclass(frozen=True)
class FaceMatch:
    """Result of matching a detected face against the enrollment database."""

    name: str
    confidence: float  # cosine similarity score
    detection: FaceDetection


class FaceRecognizer:
    """SFace face recognizer with enrollment and matching.

    Args:
        model_path: Path to SFace ONNX model. Defaults to models/ directory.
        match_threshold: Minimum cosine similarity for a match (R3: 0.363).
        embeddings_per_person: Number of embeddings to store per enrollment.
        event_bus: NucEventBus for emitting FACE_RECOGNIZED events.
    """

    def __init__(
        self,
        model_path: str | None = None,
        match_threshold: float = 0.363,
        embeddings_per_person: int = 5,
        event_bus: NucEventBus | None = None,
    ) -> None:
        if model_path is None:
            model_path = os.path.join(
                os.path.abspath(_DEFAULT_MODEL_DIR), _SFACE_FILENAME,
            )
        self._model_path = model_path
        self._match_threshold = match_threshold
        self._embeddings_per_person = embeddings_per_person
        self._event_bus = event_bus
        self._recognizer = None  # lazy-loaded
        self._lock = threading.Lock()

        # Database: name -> list of embedding arrays
        self._database: dict[str, list[np.ndarray]] = {}

    def _load_model(self) -> None:
        """Lazy-load the SFace model via OpenCV."""
        import cv2

        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(
                f"SFace model not found: {self._model_path}. "
                "Run: python3 scripts/export-openvino-models.py"
            )

        self._recognizer = cv2.FaceRecognizerSF.create(
            model=self._model_path,
            config="",
        )

        logger.info(
            "SFace loaded: model=%s, threshold=%.3f",
            os.path.basename(self._model_path),
            self._match_threshold,
        )

    def _align_face(self, frame: np.ndarray, detection: FaceDetection) -> np.ndarray:
        """Align and crop a face for embedding extraction.

        SFace expects the aligned face from FaceDetectorYN output.
        We reconstruct the detection array in YuNet format for alignCrop.
        """
        # Reconstruct YuNet-format array for alignCrop
        det_array = np.array([[
            detection.x, detection.y, detection.width, detection.height,
            *[coord for lm in detection.landmarks for coord in lm],
            detection.confidence,
        ]], dtype=np.float32)

        return self._recognizer.alignCrop(frame, det_array[0])

    def _extract_embedding(self, aligned_face: np.ndarray) -> np.ndarray:
        """Extract a 128-D embedding from an aligned face image."""
        return self._recognizer.feature(aligned_face)

    def recognize(
        self,
        frame: np.ndarray,
        detections: list[FaceDetection],
    ) -> list[FaceMatch]:
        """Recognize faces in a frame against the enrollment database.

        Args:
            frame: BGR image as numpy array.
            detections: Face detections from FaceDetector.detect().

        Returns:
            List of FaceMatch results (one per detection that matched).
            Unmatched detections are returned with name="unknown".
        """
        if self._recognizer is None:
            self._load_model()

        results = []
        for det in detections:
            aligned = self._align_face(frame, det)
            embedding = self._extract_embedding(aligned)

            name, confidence = self._match_embedding(embedding)

            match = FaceMatch(name=name, confidence=confidence, detection=det)
            results.append(match)

            if name != "unknown" and self._event_bus is not None:
                self._event_bus.emit(FACE_RECOGNIZED, FaceRecognizedEvent(
                    name=name,
                    confidence=confidence,
                    x=det.x,
                    y=det.y,
                    width=det.width,
                    height=det.height,
                ))

        return results

    def _match_embedding(self, embedding: np.ndarray) -> tuple[str, float]:
        """Match an embedding against the database.

        Returns:
            Tuple of (name, confidence). Returns ("unknown", 0.0) if no match.
        """
        with self._lock:
            best_name = "unknown"
            best_score = 0.0

            for name, embeddings in self._database.items():
                for stored in embeddings:
                    score = float(self._cosine_similarity(embedding, stored))
                    if score > best_score:
                        best_score = score
                        best_name = name

            if best_score < self._match_threshold:
                return "unknown", best_score

            return best_name, best_score

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings."""
        a_flat = a.flatten()
        b_flat = b.flatten()
        dot = np.dot(a_flat, b_flat)
        norm_a = np.linalg.norm(a_flat)
        norm_b = np.linalg.norm(b_flat)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    def enroll(
        self,
        name: str,
        frame: np.ndarray,
        detections: list[FaceDetection],
    ) -> int:
        """Enroll a face by storing embeddings from detected faces.

        Takes up to embeddings_per_person embeddings from the provided
        detections. Call multiple times with different frames to build
        up the enrollment set.

        Args:
            name: Person's name identifier.
            frame: BGR image containing the face.
            detections: Face detections (typically from FaceDetector).

        Returns:
            Total number of embeddings stored for this person.
        """
        if self._recognizer is None:
            self._load_model()

        with self._lock:
            if name not in self._database:
                self._database[name] = []

            for det in detections:
                if len(self._database[name]) >= self._embeddings_per_person:
                    break
                aligned = self._align_face(frame, det)
                embedding = self._extract_embedding(aligned)
                self._database[name].append(embedding.copy())

            count = len(self._database[name])
            logger.info(
                "Enrolled '%s': %d/%d embeddings",
                name, count, self._embeddings_per_person,
            )
            return count

    def remove(self, name: str) -> bool:
        """Remove a person from the enrollment database.

        Returns:
            True if the person was found and removed.
        """
        with self._lock:
            if name in self._database:
                del self._database[name]
                logger.info("Removed '%s' from face database", name)
                return True
            return False

    def list_enrolled(self) -> dict[str, int]:
        """Return enrolled names and their embedding counts."""
        with self._lock:
            return {name: len(embs) for name, embs in self._database.items()}

    def save_database(self, path: str | None = None) -> None:
        """Save the enrollment database to a JSON file.

        Embeddings are stored as lists for JSON serialization.
        """
        if path is None:
            os.makedirs(os.path.abspath(_DEFAULT_DB_DIR), exist_ok=True)
            path = os.path.join(
                os.path.abspath(_DEFAULT_DB_DIR), _DEFAULT_DB_FILENAME,
            )

        with self._lock:
            data = {
                name: [emb.tolist() for emb in embeddings]
                for name, embeddings in self._database.items()
            }

        with open(path, "w") as f:
            json.dump(data, f)

        logger.info("Saved face database to %s (%d people)", path, len(data))

    def load_database(self, path: str | None = None) -> None:
        """Load the enrollment database from a JSON file."""
        if path is None:
            path = os.path.join(
                os.path.abspath(_DEFAULT_DB_DIR), _DEFAULT_DB_FILENAME,
            )

        if not os.path.isfile(path):
            logger.warning("Face database not found: %s", path)
            return

        with open(path) as f:
            data = json.load(f)

        with self._lock:
            self._database = {
                name: [np.array(emb, dtype=np.float32) for emb in embeddings]
                for name, embeddings in data.items()
            }

        logger.info("Loaded face database from %s (%d people)", path, len(data))

    @property
    def match_threshold(self) -> float:
        return self._match_threshold

    @match_threshold.setter
    def match_threshold(self, value: float) -> None:
        self._match_threshold = value

    @property
    def enrolled_count(self) -> int:
        """Number of enrolled people."""
        with self._lock:
            return len(self._database)

    @property
    def is_loaded(self) -> bool:
        return self._recognizer is not None
