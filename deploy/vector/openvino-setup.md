# OpenVINO ML Inference Setup — NUC

## Hardware

| Component | Spec |
|-----------|------|
| CPU | Intel i7-1360P (16 threads, AVX2/VNNI) |
| iGPU | Intel Iris Xe (96 EU) |
| RAM | 32 GB DDR5 |
| CUDA | **None** — no NVIDIA GPU |

OpenVINO replaces CUDA as the ML inference backend.

## Quick Start

```bash
# 1. Install dependencies
bash scripts/openvino-setup.sh

# 2. Export models (downloads YOLO11s, YuNet, SFace)
python3 scripts/export-openvino-models.py

# 3. Benchmark (target: 15+ FPS for YOLO11s)
python3 scripts/benchmark-openvino.py

# 4. Validate detection pipeline
python3 apps/vector/tests/standalone/test_detection.py
```

## Python Packages

| Package | Purpose |
|---------|---------|
| `openvino` | Intel ML inference runtime |
| `ultralytics` | YOLO model loading, export, inference |
| `opencv-python-headless` | Image processing, YuNet/SFace inference |
| `onnxruntime-openvino` | ONNX models with OpenVINO backend |

## Models

All models live at `apps/vector/models/` (git-ignored, download via script).

| Model | Format | Purpose | Source |
|-------|--------|---------|--------|
| YOLO11s | OpenVINO IR (.xml/.bin) | Person detection | ultralytics export |
| YuNet | ONNX | Face detection | OpenCV Zoo |
| SFace | ONNX | Face recognition | OpenCV Zoo |

### YOLO11s Export

ultralytics handles the PyTorch → OpenVINO IR conversion:

```python
from ultralytics import YOLO
model = YOLO("yolo11s.pt")
model.export(format="openvino", imgsz=640)
# Creates yolo11s_openvino_model/ with .xml + .bin
```

### Face Models

YuNet and SFace use OpenCV's built-in DNN module (no separate OpenVINO conversion needed):

```python
import cv2
face_detector = cv2.FaceDetectorYN.create("face_detection_yunet_2023mar.onnx", "", (320, 320))
face_recognizer = cv2.FaceRecognizerSF.create("face_recognition_sface_2021dec.onnx", "")
```

## OpenVINO Devices

```python
import openvino as ov
core = ov.Core()
print(core.available_devices)
# Expected: ['CPU', 'GPU'] (GPU = Iris Xe iGPU)
```

- **CPU**: Always available. Uses AVX2/VNNI for fast inference.
- **GPU**: Requires Intel GPU compute drivers (`intel-opencl-icd`). Install with:
  ```bash
  sudo apt install intel-opencl-icd
  ```

## Benchmark Results Template

Run `python3 scripts/benchmark-openvino.py` and record results here:

```
Model: YOLO11s (640x640 input)
Image: 640x360 (Vector camera resolution)
Frames: 100

OpenVINO Core API:
  CPU:  __ ms avg (__ FPS)
  GPU:  __ ms avg (__ FPS)

Ultralytics API:
  CPU:  __ ms avg (__ FPS)
```

Target: 15+ FPS on CPU for real-time person detection.

## Downstream Issues

- **#11** — YOLO person detection (uses YOLO11s OpenVINO model)
- **#12** — Face recognition (uses YuNet + SFace ONNX models)
- **#20** — STT/Whisper (optional OpenVINO export via optimum-intel)
