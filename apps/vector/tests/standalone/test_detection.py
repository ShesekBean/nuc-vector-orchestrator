#!/usr/bin/env python3
"""Standalone detection test — YOLO inference on saved frames.

Supports both OpenVINO IR models (preferred) and PyTorch .pt models.
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


def find_yolo_model():
    """Find the best available YOLO model, preferring OpenVINO."""
    # Prefer OpenVINO IR model (fastest on Intel)
    ov_dir = os.path.join(MODEL_DIR, "yolo11s_openvino_model")
    if os.path.isdir(ov_dir):
        xml_files = [f for f in os.listdir(ov_dir) if f.endswith(".xml")]
        if xml_files:
            return ov_dir, "openvino"

    # Fall back to .pt models
    for name in ["yolo11s.pt", "yolov8n.pt", "yolov5n.pt"]:
        path = os.path.join(MODEL_DIR, name)
        if os.path.exists(path):
            return path, "pytorch"

    # Fall back to ONNX
    for name in ["yolo11s.onnx", "yolov8n.onnx", "yolov5n.onnx"]:
        path = os.path.join(MODEL_DIR, name)
        if os.path.exists(path):
            return path, "onnx"

    return None, None


def main():
    print("=" * 60)
    print("STANDALONE TEST: Detection (YOLO, OpenVINO preferred)")
    print("=" * 60)
    print("  Note: No robot connection needed\n")

    results = []

    # Sub-test 1: Check if OpenVINO is available
    def test_openvino():
        import openvino as ov

        core = ov.Core()
        devices = core.available_devices
        return f"OpenVINO {ov.__version__}, devices: {devices}"

    results.append(run_test("OpenVINO import", test_openvino))

    # Sub-test 2: Check if PIL and numpy are available
    def test_deps():
        from PIL import Image  # noqa: F401
        import numpy as np  # noqa: F401

        return f"Pillow + numpy {np.__version__}"

    results.append(run_test("Dependencies", test_deps))

    # Sub-test 3: Check for YOLO model file
    def test_model_exists():
        model_path, model_type = find_yolo_model()
        if model_path:
            if model_type == "openvino":
                return f"OpenVINO IR model at {os.path.basename(model_path)}/"
            size_mb = os.path.getsize(model_path) / (1024 * 1024)
            return f"found {os.path.basename(model_path)} ({size_mb:.1f} MB, type={model_type})"
        return "no model file found (run scripts/export-openvino-models.py)"

    results.append(run_test("YOLO model file check", test_model_exists))

    # Sub-test 4: Check if ultralytics is available
    def test_ultralytics():
        try:
            from ultralytics import YOLO  # noqa: F401

            return "ultralytics available"
        except ImportError:
            return "ultralytics not installed (optional for OpenVINO direct)"

    results.append(run_test("Ultralytics import", test_ultralytics))

    # Sub-test 5: Create a synthetic test image and run detection
    def test_synthetic_detection():
        import numpy as np
        from PIL import Image

        # Create a 640x360 synthetic image (matches Vector camera resolution)
        img_array = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)
        img = Image.fromarray(img_array)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = os.path.join(OUTPUT_DIR, "synthetic_test.png")
        img.save(path)

        model_path, model_type = find_yolo_model()

        if model_path:
            from ultralytics import YOLO

            model = YOLO(model_path)
            det_results = model(img_array, verbose=False)
            n_det = len(det_results[0].boxes) if det_results else 0
            return f"detection ran ({model_type}), {n_det} objects on noise image"

        # No model — just verify image pipeline
        return f"synthetic image saved ({path}), no model to run detection"

    results.append(run_test("Synthetic image detection", test_synthetic_detection))

    # Sub-test 6: Test on captured camera image if it exists
    def test_saved_frame():
        sample_path = os.path.join(OUTPUT_DIR, "camera_sample.png")
        if not os.path.exists(sample_path):
            return "no camera_sample.png (run test_camera.py first)"

        from PIL import Image

        img = Image.open(sample_path)
        w, h = img.size

        model_path, _ = find_yolo_model()
        if model_path:
            import numpy as np
            from ultralytics import YOLO

            model = YOLO(model_path)
            det_results = model(np.array(img), verbose=False)
            n_det = len(det_results[0].boxes) if det_results else 0
            return f"{w}x{h} image, {n_det} detections"

        return f"{w}x{h} image loaded, no model available"

    results.append(run_test("Saved camera frame detection", test_saved_frame))

    # Sub-test 7: Quick FPS estimate (5 frames)
    def test_fps_estimate():
        model_path, model_type = find_yolo_model()
        if not model_path:
            return "skipped — no model available"

        import numpy as np
        from ultralytics import YOLO

        model = YOLO(model_path)
        img = np.random.randint(0, 255, (360, 640, 3), dtype=np.uint8)

        # Warmup
        model(img, verbose=False)

        # Time 5 frames
        t0 = time.time()
        for _ in range(5):
            model(img, verbose=False)
        elapsed = time.time() - t0

        fps = 5.0 / elapsed
        target = "PASS" if fps >= 15 else "BELOW TARGET"
        return f"{fps:.1f} FPS ({model_type}) — {target} (target: 15+ FPS)"

    results.append(run_test("FPS estimate (5 frames)", test_fps_estimate))

    # Sub-test 8: Face detection model check
    def test_face_models():
        found = []
        for name in ["face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"]:
            path = os.path.join(MODEL_DIR, name)
            if os.path.exists(path):
                found.append(name.split("_")[2])  # "yunet" or "sface"
        if found:
            return f"found: {', '.join(found)}"
        return "no face models (run scripts/export-openvino-models.py)"

    results.append(run_test("Face model files", test_face_models))

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Detection: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
