#!/usr/bin/env python3
"""Benchmark camera frame rates from Vector.

Measures SDK event-based streaming vs single-capture performance,
including FPS, per-frame latency, and memory usage.

Usage:
    python3 -m apps.vector.src.camera.camera_benchmark [--duration 10] [--serial 0dd1cdcf]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

# Ensure repo root is on sys.path for imports
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _get_process_memory_mb() -> float:
    """Return current process RSS in MB (Linux only)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def benchmark_event_stream(robot: object, duration: float) -> dict:
    """Benchmark SDK event-based camera streaming."""
    from apps.vector.src.camera.camera_client import CameraClient

    print(f"\n{'='*60}")
    print(f"SDK Event Stream Benchmark ({duration:.0f}s)")
    print(f"{'='*60}")

    mem_before = _get_process_memory_mb()
    client = CameraClient(robot, buffer_size=30)
    client.start()

    # Wait for frames to flow
    time.sleep(0.5)
    start_count = client.frame_count
    start_time = time.monotonic()

    # Collect per-frame latencies by sampling
    latencies: list[float] = []

    while time.monotonic() - start_time < duration:
        t0 = time.monotonic()
        frame = client.get_latest_frame()
        lat = time.monotonic() - t0
        if frame is not None:
            latencies.append(lat)
        time.sleep(0.05)  # 20Hz polling to not starve SDK thread

    elapsed = time.monotonic() - start_time
    total_frames = client.frame_count - start_count
    rolling_fps = client.fps
    mem_after = _get_process_memory_mb()

    client.stop()

    # Get frame dimensions from last frame
    frame = client.get_latest_frame()
    resolution = f"{frame.shape[1]}x{frame.shape[0]}" if frame is not None else "unknown"

    results = {
        "method": "SDK Event Stream",
        "duration_s": round(elapsed, 2),
        "total_frames": total_frames,
        "avg_fps": round(total_frames / elapsed, 2) if elapsed > 0 else 0,
        "rolling_fps": round(rolling_fps, 2),
        "get_frame_latency_ms": round(np.mean(latencies) * 1000, 3) if latencies else 0,
        "get_frame_p99_ms": round(np.percentile(latencies, 99) * 1000, 3) if latencies else 0,
        "memory_delta_mb": round(mem_after - mem_before, 2),
        "memory_rss_mb": round(mem_after, 2),
        "resolution": resolution,
    }

    for k, v in results.items():
        print(f"  {k}: {v}")

    return results


def benchmark_single_capture(robot: object, num_captures: int) -> dict:
    """Benchmark single-image capture method."""
    import cv2

    print(f"\n{'='*60}")
    print(f"Single Capture Benchmark ({num_captures} captures)")
    print(f"{'='*60}")

    mem_before = _get_process_memory_mb()
    latencies: list[float] = []
    resolution = "unknown"

    for i in range(num_captures):
        t0 = time.monotonic()
        image = robot.camera.capture_single_image()
        if image is not None:
            pil_img = image.raw_image
            rgb = np.asarray(pil_img)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            lat = time.monotonic() - t0
            latencies.append(lat)
            if resolution == "unknown":
                resolution = f"{bgr.shape[1]}x{bgr.shape[0]}"

    mem_after = _get_process_memory_mb()
    total_elapsed = sum(latencies)

    results = {
        "method": "Single Capture",
        "num_captures": num_captures,
        "total_time_s": round(total_elapsed, 2),
        "avg_fps": round(len(latencies) / total_elapsed, 2) if total_elapsed > 0 else 0,
        "avg_latency_ms": round(np.mean(latencies) * 1000, 2) if latencies else 0,
        "p99_latency_ms": round(np.percentile(latencies, 99) * 1000, 2) if latencies else 0,
        "min_latency_ms": round(np.min(latencies) * 1000, 2) if latencies else 0,
        "max_latency_ms": round(np.max(latencies) * 1000, 2) if latencies else 0,
        "memory_delta_mb": round(mem_after - mem_before, 2),
        "memory_rss_mb": round(mem_after, 2),
        "resolution": resolution,
    }

    for k, v in results.items():
        print(f"  {k}: {v}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Vector camera methods")
    parser.add_argument("--duration", type=float, default=10.0, help="Stream benchmark duration (seconds)")
    parser.add_argument("--captures", type=int, default=30, help="Number of single captures")
    parser.add_argument("--serial", type=str, default="0dd1cdcf", help="Vector serial number")
    args = parser.parse_args()

    import anki_vector

    print(f"Connecting to Vector (serial={args.serial})...")
    robot = anki_vector.Robot(serial=args.serial, default_logging=False)
    robot.connect()

    try:
        # Benchmark 1: SDK event stream
        event_results = benchmark_event_stream(robot, args.duration)

        # Small gap between benchmarks
        time.sleep(1.0)

        # Benchmark 2: Single capture
        single_results = benchmark_single_capture(robot, args.captures)

        # Summary comparison
        print(f"\n{'='*60}")
        print("COMPARISON SUMMARY")
        print(f"{'='*60}")
        print(f"  {'Metric':<25} {'Event Stream':>15} {'Single Capture':>15}")
        print(f"  {'-'*55}")
        print(f"  {'Avg FPS':<25} {event_results['avg_fps']:>15} {single_results['avg_fps']:>15}")
        print(f"  {'Resolution':<25} {event_results['resolution']:>15} {single_results['resolution']:>15}")
        print(f"  {'Memory RSS (MB)':<25} {event_results['memory_rss_mb']:>15} {single_results['memory_rss_mb']:>15}")

    finally:
        robot.disconnect()
        print("\nDisconnected from Vector.")


if __name__ == "__main__":
    main()
