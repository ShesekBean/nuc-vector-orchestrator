#!/usr/bin/env bash
# Install OpenVINO runtime and ML dependencies on the NUC.
# Run once after cloning the repo.
#
# Usage: bash scripts/openvino-setup.sh

set -euo pipefail

echo "=== OpenVINO ML Inference Setup ==="
echo "Target: Intel i7-1360P (AVX2/VNNI) + Iris Xe iGPU"
echo ""

# ---------- Python packages ----------
echo "[1/4] Installing OpenVINO runtime..."
pip install --quiet openvino

echo "[2/4] Installing ultralytics (YOLO)..."
pip install --quiet ultralytics

echo "[3/4] Installing OpenCV headless..."
pip install --quiet opencv-python-headless

echo "[4/4] Installing ONNX runtime (fallback backend)..."
pip install --quiet onnxruntime-openvino

# ---------- Verify imports ----------
echo ""
echo "=== Verifying installations ==="

python3 -c "
import openvino as ov
print(f'  OpenVINO: {ov.__version__}')
core = ov.Core()
devices = core.available_devices
print(f'  Available devices: {devices}')
"

python3 -c "
from ultralytics import YOLO
print('  ultralytics: OK')
"

python3 -c "
import cv2
print(f'  OpenCV: {cv2.__version__}')
"

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Export models:    python3 scripts/export-openvino-models.py"
echo "  2. Run benchmarks:   python3 scripts/benchmark-openvino.py"
