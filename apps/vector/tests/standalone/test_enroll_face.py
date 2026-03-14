#!/usr/bin/env python3
"""Interactive face + body enrollment — capture from Vector's camera.

Connects to Vector, captures frames with you in view, detects face + body,
enrolls face embeddings, and saves reference body crops.

Run: python3 apps/vector/tests/standalone/test_enroll_face.py [name]
  Default name: ophir
  Captures 5 frames interactively (press Enter for each)
  Saves face embeddings to apps/vector/data/face_database.json
  Saves body reference crops to apps/vector/data/reference_images/<name>/
"""

import os
import sys
import time

import cv2
import numpy as np

# Ensure repo root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

import anki_vector

from apps.vector.src.face_recognition.face_detector import FaceDetector
from apps.vector.src.face_recognition.face_recognizer import FaceRecognizer
from apps.vector.src.detector.person_detector import PersonDetector

SERIAL = "0dd1cdcf"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
REF_IMG_DIR = os.path.join(DATA_DIR, "reference_images")
NUM_FRAMES = 5


def pil_to_bgr(pil_image):
    """Convert PIL Image to OpenCV BGR numpy array."""
    rgb = np.array(pil_image)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def draw_detections(frame, faces, persons, name):
    """Draw face and person bounding boxes on frame for preview."""
    vis = frame.copy()

    for det in persons:
        x1 = int(det.cx - det.width / 2)
        y1 = int(det.cy - det.height / 2)
        x2 = int(det.cx + det.width / 2)
        y2 = int(det.cy + det.height / 2)
        cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(vis, f"body {det.confidence:.2f}", (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    for face in faces:
        x, y = int(face.x), int(face.y)
        w, h = int(face.width), int(face.height)
        cv2.rectangle(vis, (x, y), (x + w, y + h), (255, 0, 0), 2)
        cv2.putText(vis, f"face {face.confidence:.2f}", (x, y - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

    cv2.putText(vis, f"Enrolling: {name}", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    return vis


def crop_person(frame, detection):
    """Crop person bounding box from frame."""
    h, w = frame.shape[:2]
    x1 = max(0, int(detection.cx - detection.width / 2))
    y1 = max(0, int(detection.cy - detection.height / 2))
    x2 = min(w, int(detection.cx + detection.width / 2))
    y2 = min(h, int(detection.cy + detection.height / 2))
    return frame[y1:y2, x1:x2]


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "ophir"

    print("=" * 60)
    print(f"FACE + BODY ENROLLMENT: {name}")
    print("=" * 60)

    # Init models
    print("\nLoading models...")
    face_detector = FaceDetector()
    face_recognizer = FaceRecognizer()
    person_detector = PersonDetector()

    # Load existing database if present
    db_path = os.path.join(os.path.abspath(DATA_DIR), "face_database.json")
    if os.path.isfile(db_path):
        face_recognizer.load_database(db_path)
        print(f"Loaded existing database: {face_recognizer.list_enrolled()}")

    # Create reference image directory
    person_img_dir = os.path.join(os.path.abspath(REF_IMG_DIR), name)
    os.makedirs(person_img_dir, exist_ok=True)

    # Connect to Vector
    print(f"\nConnecting to Vector ({SERIAL})...")
    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    robot.connect()
    robot.camera.init_camera_feed()
    time.sleep(1)  # Let camera stabilize

    print(f"\nReady! Stand in front of Vector.")
    print(f"Will capture {NUM_FRAMES} frames. Press Enter for each.\n")

    enrolled_faces = 0
    saved_bodies = 0

    try:
        for i in range(NUM_FRAMES):
            input(f"  Frame {i + 1}/{NUM_FRAMES} — position yourself, then press Enter...")

            # Capture
            img = robot.camera.latest_image
            if img is None:
                print("    No frame available, retrying...")
                time.sleep(0.5)
                img = robot.camera.latest_image
                if img is None:
                    print("    Still no frame. Skipping.")
                    continue

            frame = pil_to_bgr(img.raw_image)
            print(f"    Captured {frame.shape[1]}x{frame.shape[0]}")

            # Detect face
            faces = face_detector.detect(frame)
            if not faces:
                print("    No face detected! Try better lighting or angle.")
                continue

            print(f"    Found {len(faces)} face(s), best confidence: {faces[0].confidence:.3f}")

            # Enroll face embedding
            count = face_recognizer.enroll(name, frame, faces[:1])  # Best face only
            enrolled_faces = count
            print(f"    Face embeddings: {count}/{face_recognizer._embeddings_per_person}")

            # Detect body with YOLO
            persons = person_detector.detect(frame)
            if persons:
                best = persons[0]
                body_crop = crop_person(frame, best)
                if body_crop.size > 0:
                    crop_path = os.path.join(person_img_dir, f"body_{i + 1}.jpg")
                    cv2.imwrite(crop_path, body_crop)
                    saved_bodies += 1
                    print(f"    Body crop saved: {crop_path} ({body_crop.shape[1]}x{body_crop.shape[0]})")
            else:
                print("    No body detected by YOLO (try standing further back)")

            # Save annotated preview
            vis = draw_detections(frame, faces, persons, name)
            preview_path = os.path.join(person_img_dir, f"preview_{i + 1}.jpg")
            cv2.imwrite(preview_path, vis)

            # Also save full frame as reference
            full_path = os.path.join(person_img_dir, f"full_{i + 1}.jpg")
            cv2.imwrite(full_path, frame)

    finally:
        robot.camera.close_camera_feed()
        robot.disconnect()

    # Save face database
    os.makedirs(os.path.abspath(DATA_DIR), exist_ok=True)
    face_recognizer.save_database(db_path)

    print(f"\n{'=' * 60}")
    print(f"ENROLLMENT COMPLETE: {name}")
    print(f"  Face embeddings: {enrolled_faces}")
    print(f"  Body crops saved: {saved_bodies}")
    print(f"  Reference images: {person_img_dir}")
    print(f"  Face database: {db_path}")
    print(f"  All enrolled: {face_recognizer.list_enrolled()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
