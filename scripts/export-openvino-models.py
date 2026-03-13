#!/usr/bin/env python3
"""Export ML models to OpenVINO IR format.

Exports:
  1. YOLO11s  → OpenVINO IR (person detection)
  2. YuNet    → ONNX (face detection — already OpenCV-native)
  3. SFace    → ONNX (face recognition — already OpenCV-native)

Usage: python3 scripts/export-openvino-models.py
"""

import os
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
MODEL_DIR = os.path.join(REPO_ROOT, "apps", "vector", "models")

# YuNet and SFace ONNX URLs (OpenCV Zoo)
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)


def export_yolo(model_dir: str) -> None:
    """Export YOLO11n (nano) to OpenVINO IR format.

    YOLO11n chosen over YOLO11s for speed: 47ms vs 97ms on NUC OpenVINO.
    Accuracy tradeoff is acceptable — Vector's dark OV7251 camera limits
    detection quality regardless of model size.
    """
    from ultralytics import YOLO

    print("[1/3] Exporting YOLO11n to OpenVINO IR...")

    # Work in model_dir so downloads and exports land there
    prev_dir = os.getcwd()
    os.chdir(model_dir)
    try:
        model = YOLO("yolo11n.pt")
        export_path = model.export(format="openvino", imgsz=640, half=False)
        print(f"  Exported to: {export_path}")
    finally:
        os.chdir(prev_dir)

    print("  YOLO11n export complete.")


def download_file(url: str, dest: str) -> None:
    """Download a file if it doesn't already exist."""
    if os.path.exists(dest):
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        print(f"  Already exists: {dest} ({size_mb:.1f} MB)")
        return
    print(f"  Downloading: {os.path.basename(dest)}...")
    urllib.request.urlretrieve(url, dest)
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"  Saved: {dest} ({size_mb:.1f} MB)")


def download_face_models(model_dir: str) -> None:
    """Download YuNet and SFace ONNX models from OpenCV Zoo."""
    print("[2/3] Downloading YuNet (face detection)...")
    download_file(YUNET_URL, os.path.join(model_dir, "face_detection_yunet_2023mar.onnx"))

    print("[3/3] Downloading SFace (face recognition)...")
    download_file(SFACE_URL, os.path.join(model_dir, "face_recognition_sface_2021dec.onnx"))


def verify_models(model_dir: str) -> bool:
    """Verify all models are present and loadable."""
    print("\n=== Verification ===")
    ok = True

    # Check YOLO OpenVINO model
    yolo_dir = os.path.join(model_dir, "yolo11n_openvino_model")
    xml_files = [f for f in os.listdir(yolo_dir) if f.endswith(".xml")] if os.path.isdir(yolo_dir) else []
    if xml_files:
        print(f"  YOLO11s OpenVINO: OK ({xml_files[0]})")
    else:
        print("  YOLO11s OpenVINO: MISSING")
        ok = False

    # Check face models
    for name in ["face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"]:
        path = os.path.join(model_dir, name)
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            print(f"  {name}: OK ({size_mb:.1f} MB)")
        else:
            print(f"  {name}: MISSING")
            ok = False

    # Test OpenVINO can load the YOLO model
    if xml_files:
        try:
            import openvino as ov

            core = ov.Core()
            xml_path = os.path.join(yolo_dir, xml_files[0])
            core.read_model(xml_path)
            print("  OpenVINO load test: OK")
        except Exception as e:
            print(f"  OpenVINO load test: FAIL — {e}")
            ok = False

    # Test OpenCV can load face models
    try:
        import cv2

        yunet_path = os.path.join(model_dir, "face_detection_yunet_2023mar.onnx")
        if os.path.exists(yunet_path):
            cv2.FaceDetectorYN.create(yunet_path, "", (320, 320))
            print("  YuNet OpenCV load test: OK")

        sface_path = os.path.join(model_dir, "face_recognition_sface_2021dec.onnx")
        if os.path.exists(sface_path):
            cv2.FaceRecognizerSF.create(sface_path, "")
            print("  SFace OpenCV load test: OK")
    except Exception as e:
        print(f"  Face model OpenCV load test: FAIL — {e}")
        ok = False

    return ok


def main() -> int:
    os.makedirs(MODEL_DIR, exist_ok=True)
    print(f"Model directory: {MODEL_DIR}\n")

    export_yolo(MODEL_DIR)
    download_face_models(MODEL_DIR)

    if verify_models(MODEL_DIR):
        print("\n=== All models ready ===")
        return 0
    else:
        print("\n=== Some models failed verification ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
