#!/usr/bin/env python3
"""Standalone camera test — capture frames, measure FPS, save sample image.

Run: python3 apps/vector/tests/standalone/test_camera.py
"""

import os
import sys
import time

import anki_vector

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


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
    print("STANDALONE TEST: Camera")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    try:
        robot.connect()
        print(f"Connected to {robot.name}\n")

        # Sub-test 1: Capture single image
        def test_single_capture():
            image = robot.camera.capture_single_image()
            if image is None:
                raise RuntimeError("capture_single_image returned None")
            w, h = image.raw_image.size
            return f"{w}x{h}"

        results.append(run_test("Single image capture", test_single_capture))

        # Sub-test 2: Save captured image to disk
        def test_save_image():
            image = robot.camera.capture_single_image()
            path = os.path.join(SAMPLE_DIR, "camera_sample.png")
            image.raw_image.save(path)
            size_kb = os.path.getsize(path) / 1024
            return f"saved {path} ({size_kb:.1f} KB)"

        results.append(run_test("Save image to disk", test_save_image))

        # Sub-test 3: Stream frames and measure FPS
        def test_fps():
            robot.camera.init_camera_feed()
            time.sleep(0.5)  # Let feed stabilize

            frame_count = 0
            t_start = time.time()
            duration = 3.0  # Measure over 3 seconds

            while time.time() - t_start < duration:
                img = robot.camera.latest_image
                if img is not None:
                    frame_count += 1
                time.sleep(0.03)  # ~30 Hz poll rate

            robot.camera.close_camera_feed()
            elapsed = time.time() - t_start
            fps = frame_count / elapsed if elapsed > 0 else 0
            if frame_count == 0:
                raise RuntimeError("No frames received in 3 seconds")
            return f"{fps:.1f} FPS ({frame_count} frames / {elapsed:.1f}s)"

        results.append(run_test("Stream FPS measurement (3s)", test_fps))

        # Sub-test 4: Image dimensions check
        def test_dimensions():
            image = robot.camera.capture_single_image()
            w, h = image.raw_image.size
            if w < 320 or h < 180:
                raise RuntimeError(f"Unexpectedly small: {w}x{h}")
            return f"{w}x{h}"

        results.append(run_test("Image dimensions valid", test_dimensions))

        # Sub-test 5: Multiple rapid captures
        def test_rapid_capture():
            images = []
            for _ in range(5):
                img = robot.camera.capture_single_image()
                images.append(img)
            if len(images) != 5:
                raise RuntimeError(f"Only captured {len(images)}/5")
            return "5 frames captured"

        results.append(run_test("Rapid capture (5 frames)", test_rapid_capture))

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Camera: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
