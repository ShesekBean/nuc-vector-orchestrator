#!/usr/bin/env python3
"""Benchmark OpenVINO inference speed on NUC hardware.

Tests YOLO11s inference on CPU and GPU (if available).
Uses synthetic 640x360 images (Vector camera resolution).

Usage: python3 scripts/benchmark-openvino.py
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
MODEL_DIR = os.path.join(REPO_ROOT, "apps", "vector", "models")

# Benchmark parameters
WARMUP_FRAMES = 10
BENCH_FRAMES = 100
IMG_WIDTH = 640
IMG_HEIGHT = 360


def benchmark_ultralytics(model_path: str, device: str) -> dict:
    """Benchmark YOLO via ultralytics (high-level API)."""
    import numpy as np
    from ultralytics import YOLO

    model = YOLO(model_path)

    # Generate synthetic frames
    frames = [np.random.randint(0, 255, (IMG_HEIGHT, IMG_WIDTH, 3), dtype=np.uint8) for _ in range(5)]

    # Warmup
    for i in range(WARMUP_FRAMES):
        model(frames[i % len(frames)], verbose=False, device=device)

    # Benchmark
    times = []
    for i in range(BENCH_FRAMES):
        t0 = time.perf_counter()
        model(frames[i % len(frames)], verbose=False, device=device)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

    times_ms = [t * 1000 for t in times]
    avg_ms = sum(times_ms) / len(times_ms)
    min_ms = min(times_ms)
    max_ms = max(times_ms)
    fps = 1000.0 / avg_ms

    return {
        "avg_ms": avg_ms,
        "min_ms": min_ms,
        "max_ms": max_ms,
        "fps": fps,
        "frames": BENCH_FRAMES,
    }


def benchmark_openvino_direct(model_dir: str, device: str) -> dict:
    """Benchmark YOLO via OpenVINO Core API (low-level)."""
    import numpy as np
    import openvino as ov

    core = ov.Core()

    # Find the .xml file
    xml_files = [f for f in os.listdir(model_dir) if f.endswith(".xml")]
    if not xml_files:
        raise FileNotFoundError(f"No .xml model in {model_dir}")

    xml_path = os.path.join(model_dir, xml_files[0])
    model = core.read_model(xml_path)
    compiled = core.compile_model(model, device)
    infer_request = compiled.create_infer_request()

    # Get input shape
    input_layer = compiled.input(0)
    input_shape = input_layer.shape  # e.g. [1, 3, 640, 640]
    h, w = input_shape[2], input_shape[3]

    # Generate synthetic input
    frames = [np.random.randn(1, 3, h, w).astype(np.float32) for _ in range(5)]

    # Warmup
    for i in range(WARMUP_FRAMES):
        infer_request.infer(frames[i % len(frames)])

    # Benchmark
    times = []
    for i in range(BENCH_FRAMES):
        t0 = time.perf_counter()
        infer_request.infer(frames[i % len(frames)])
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

    times_ms = [t * 1000 for t in times]
    avg_ms = sum(times_ms) / len(times_ms)
    min_ms = min(times_ms)
    max_ms = max(times_ms)
    fps = 1000.0 / avg_ms

    return {
        "avg_ms": avg_ms,
        "min_ms": min_ms,
        "max_ms": max_ms,
        "fps": fps,
        "frames": BENCH_FRAMES,
    }


def print_result(label: str, result: dict) -> None:
    print(f"  {label}:")
    print(f"    Avg: {result['avg_ms']:.1f} ms  ({result['fps']:.1f} FPS)")
    print(f"    Min: {result['min_ms']:.1f} ms  Max: {result['max_ms']:.1f} ms")
    print(f"    Frames: {result['frames']}")


def main() -> int:
    print("=" * 60)
    print("OpenVINO Inference Benchmark")
    print("=" * 60)

    # Check available devices
    import openvino as ov

    core = ov.Core()
    devices = core.available_devices
    print(f"Available devices: {devices}")
    print(f"Image size: {IMG_WIDTH}x{IMG_HEIGHT} (Vector camera)")
    print(f"Warmup: {WARMUP_FRAMES} frames, Benchmark: {BENCH_FRAMES} frames")
    print("")

    ov_model_dir = os.path.join(MODEL_DIR, "yolo11s_openvino_model")
    if not os.path.isdir(ov_model_dir):
        print(f"ERROR: OpenVINO model not found at {ov_model_dir}")
        print("Run: python3 scripts/export-openvino-models.py")
        return 1

    # --- OpenVINO Core API benchmarks ---
    print("--- OpenVINO Core API (direct) ---")
    try:
        result = benchmark_openvino_direct(ov_model_dir, "CPU")
        print_result("CPU", result)
    except Exception as e:
        print(f"  CPU: FAILED — {e}")

    if "GPU" in devices:
        try:
            result = benchmark_openvino_direct(ov_model_dir, "GPU")
            print_result("GPU (Iris Xe)", result)
        except Exception as e:
            print(f"  GPU: FAILED — {e}")
    else:
        print("  GPU: not available (install Intel GPU drivers for Iris Xe)")

    print("")

    # --- Ultralytics API benchmarks ---
    print("--- Ultralytics API (high-level) ---")
    try:
        result = benchmark_ultralytics(ov_model_dir, "cpu")
        print_result("CPU (ultralytics + OpenVINO)", result)
    except Exception as e:
        print(f"  CPU ultralytics: FAILED — {e}")

    print("")
    print("=" * 60)
    print("Target: 15+ FPS for YOLO11s on CPU")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
