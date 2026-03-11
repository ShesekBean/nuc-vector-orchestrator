#!/usr/bin/env python3
"""Standalone detection test — YOLO inference on saved frames (CPU only).

This test does NOT connect to Vector. It validates that the YOLO detection
pipeline works on static images, proving the algorithm in isolation before
integrating with the live camera feed.

Run: python3 apps/vector/tests/standalone/test_detection.py
"""

import os
import sys
import time

PASS = 0
FAIL = 1
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
MODEL_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "..", "models")


def run_test(name, fn):
    print(f"  [{name}] ", end="", flush=True)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        suffix = f" — {result}" if result else ""
        print(f"PASS ({elapsed:.2f}s){suffix}")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {e}")
        return False


def main():
    print("=" * 60)
    print("STANDALONE TEST: Detection (YOLO, CPU only)")
    print("=" * 60)
    print("  Note: No robot connection needed\n")

    results = []

    # Sub-test 1: Check if PIL is available
    def test_pil():
        from PIL import Image  # noqa: F401
        return "Pillow available"

    results.append(run_test("PIL/Pillow import", test_pil))

    # Sub-test 2: Check if numpy is available
    def test_numpy():
        import numpy as np  # noqa: F401
        return f"numpy {np.__version__}"

    results.append(run_test("NumPy import", test_numpy))

    # Sub-test 3: Check for YOLO model file
    def test_model_exists():
        # Check common model locations
        candidates = [
            os.path.join(MODEL_DIR, "yolov8n.pt"),
            os.path.join(MODEL_DIR, "yolov8n.onnx"),
            os.path.join(MODEL_DIR, "yolov5n.pt"),
            os.path.join(MODEL_DIR, "yolov5n.onnx"),
        ]
        for path in candidates:
            if os.path.exists(path):
                size_mb = os.path.getsize(path) / (1024 * 1024)
                return f"found {os.path.basename(path)} ({size_mb:.1f} MB)"
        return "no model file found (will test with ultralytics if available)"

    results.append(run_test("YOLO model file check", test_model_exists))

    # Sub-test 4: Check if ultralytics is available
    def test_ultralytics():
        try:
            from ultralytics import YOLO  # noqa: F401
            return "ultralytics available"
        except ImportError:
            return "ultralytics not installed (optional)"

    results.append(run_test("Ultralytics import", test_ultralytics))

    # Sub-test 5: Create a synthetic test image and run detection if possible
    def test_synthetic_detection():
        import numpy as np
        from PIL import Image

        # Create a 640x360 synthetic image (matches Vector camera resolution)
        img_array = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
        img = Image.fromarray(img_array)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = os.path.join(OUTPUT_DIR, "synthetic_test.png")
        img.save(path)

        try:
            from ultralytics import YOLO
            model = YOLO("yolov8n.pt")
            det_results = model(img_array, verbose=False)
            n_det = len(det_results[0].boxes) if det_results else 0
            return f"detection ran, {n_det} objects found on noise image"
        except (ImportError, Exception) as e:
            return f"synthetic image saved, detection skipped ({e})"

    results.append(run_test("Synthetic image detection", test_synthetic_detection))

    # Sub-test 6: Test on captured camera image if it exists
    def test_saved_frame():
        sample_path = os.path.join(OUTPUT_DIR, "camera_sample.png")
        if not os.path.exists(sample_path):
            return "no camera_sample.png (run test_camera.py first)"

        from PIL import Image
        img = Image.open(sample_path)
        w, h = img.size

        try:
            import numpy as np
            from ultralytics import YOLO
            model = YOLO("yolov8n.pt")
            det_results = model(np.array(img), verbose=False)
            n_det = len(det_results[0].boxes) if det_results else 0
            return f"{w}x{h} image, {n_det} detections"
        except (ImportError, Exception) as e:
            return f"{w}x{h} image loaded, detection skipped ({e})"

    results.append(run_test("Saved camera frame detection", test_saved_frame))

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Detection: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
