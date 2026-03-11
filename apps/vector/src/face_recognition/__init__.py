"""Face detection (YuNet) and recognition (SFace) for Vector camera frames.

All inference runs on the NUC using OpenCV DNN with OpenVINO backend.
Vector streams camera frames via gRPC; this module processes them.
"""

from apps.vector.src.face_recognition.face_detector import (
    FaceDetection,
    FaceDetector,
)
from apps.vector.src.face_recognition.face_recognizer import (
    FaceMatch,
    FaceRecognizer,
)

__all__ = [
    "FaceDetection",
    "FaceDetector",
    "FaceMatch",
    "FaceRecognizer",
]
