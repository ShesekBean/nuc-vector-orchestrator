#!/usr/bin/env python3
"""Interactive face + body enrollment — capture from Vector's camera.

Connects to Vector, captures frames with you in view, detects face + body,
enrolls face embeddings, and saves reference body crops.

Vector speaks each pose instruction so you know when to move.
Stops the bridge service to get behavior control, restarts it after.

Run: python3 apps/vector/tests/standalone/test_enroll_face.py [name]
  Default name: ophir
"""

import os
import subprocess
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

POSES = [
    ("Look straight at me", "STRAIGHT ON"),
    ("Now turn your head left", "HEAD LEFT"),
    ("Now turn your head right", "HEAD RIGHT"),
    ("Now look up a little", "LOOK UP"),
    ("Now look down a little", "LOOK DOWN"),
]


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

    # Stop bridge to get behavior control
    print("\nStopping bridge service...")
    subprocess.run(["systemctl", "--user", "stop", "vector-bridge.service"],
                   capture_output=True)
    time.sleep(2)

    # Init models
    print("Loading models...")
    face_detector = FaceDetector()
    face_recognizer = FaceRecognizer()
    person_detector = PersonDetector()

    # Create reference image directory
    person_img_dir = os.path.join(os.path.abspath(REF_IMG_DIR), name)
    os.makedirs(person_img_dir, exist_ok=True)

    # Connect to Vector
    print(f"Connecting to Vector ({SERIAL})...")
    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    robot.connect()
    robot.behavior.set_head_angle(anki_vector.util.degrees(20))
    robot.camera.init_camera_feed()
    time.sleep(1)

    robot.behavior.say_text(f"Starting face enrollment for {name}. I will tell you how to pose.")
    time.sleep(0.5)

    enrolled_faces = 0
    saved_bodies = 0

    try:
        for i, (speech, label) in enumerate(POSES):
            # Vector speaks the pose instruction
            print(f"\n  [{i + 1}/{len(POSES)}] {label}")
            robot.behavior.say_text(speech)
            time.sleep(0.5)

            # Wait for user confirmation
            input(f"    Press Enter when ready...")

            # Capture
            img = robot.camera.latest_image
            if img is None:
                time.sleep(0.5)
                img = robot.camera.latest_image
                if img is None:
                    print("    No frame. Skipping.")
                    continue

            frame = pil_to_bgr(img.raw_image)

            # Detect face
            faces = face_detector.detect(frame)
            if not faces:
                print("    No face detected! Try better lighting.")
                robot.behavior.say_text("I can't see your face. Try again.")
                continue

            # Enroll
            count = face_recognizer.enroll(name, frame, faces[:1])
            enrolled_faces = count
            print(f"    Face: conf={faces[0].confidence:.3f}, embeddings={count}/5")

            # Body
            persons = person_detector.detect(frame)
            if persons:
                best = persons[0]
                body_crop = crop_person(frame, best)
                if body_crop.size > 0:
                    cv2.imwrite(os.path.join(person_img_dir, f"body_{i + 1}.jpg"), body_crop)
                    saved_bodies += 1
                    print(f"    Body: conf={best.confidence:.3f}, saved")

            # Save preview + full frame
            vis = draw_detections(frame, faces, persons, name)
            cv2.imwrite(os.path.join(person_img_dir, f"preview_{i + 1}.jpg"), vis)
            cv2.imwrite(os.path.join(person_img_dir, f"full_{i + 1}.jpg"), frame)

            robot.behavior.say_text("Got it!")

        robot.behavior.say_text(f"All done! I enrolled {enrolled_faces} angles for {name}.")

    finally:
        robot.camera.close_camera_feed()
        robot.disconnect()

        # Restart bridge
        print("\nRestarting bridge service...")
        subprocess.run(["systemctl", "--user", "start", "vector-bridge.service"],
                       capture_output=True)

    # Save database
    db_path = os.path.join(os.path.abspath(DATA_DIR), "face_database.json")
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
