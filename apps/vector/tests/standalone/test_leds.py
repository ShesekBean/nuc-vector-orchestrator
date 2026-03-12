#!/usr/bin/env python3
"""Standalone LED test — cycle eye colors through HSV range.

Vector's SDK exposes eye color control via set_eye_color(hue, saturation).
Backpack LEDs are not directly controllable via the high-level SDK.

Run: python3 apps/vector/tests/standalone/test_leds.py
"""

import sys
import time

import anki_vector

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1

# Named colors as (hue, saturation) — hue is 0.0-1.0
COLORS = {
    "red": (0.0, 1.0),
    "orange": (0.08, 1.0),
    "yellow": (0.17, 1.0),
    "green": (0.33, 1.0),
    "cyan": (0.5, 1.0),
    "blue": (0.67, 1.0),
    "purple": (0.78, 1.0),
    "magenta": (0.89, 1.0),
    "white": (0.0, 0.0),
}


def run_test(name, fn):
    print(f"  [{name}] ", end="", flush=True)
    t0 = time.time()
    try:
        fn()
        elapsed = time.time() - t0
        print(f"PASS ({elapsed:.2f}s)")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {e}")
        return False


def main():
    print("=" * 60)
    print("STANDALONE TEST: LEDs (Eye Color)")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print("Connected to Vector\n")

        # Sub-test 1: Cycle through named colors
        for color_name, (hue, sat) in COLORS.items():
            def test_color(h=hue, s=sat):
                robot.behavior.set_eye_color(h, s)
                time.sleep(0.8)

            results.append(run_test(f"Eye color: {color_name}", test_color))

        # Sub-test 2: HSV sweep (smooth transition)
        def test_hsv_sweep():
            for i in range(0, 101, 5):
                hue = i / 100.0
                robot.behavior.set_eye_color(hue, 1.0)
                time.sleep(0.1)

        results.append(run_test("HSV sweep (0→1 hue)", test_hsv_sweep))

        # Sub-test 3: Saturation sweep
        def test_saturation_sweep():
            for i in range(0, 101, 10):
                sat = i / 100.0
                robot.behavior.set_eye_color(0.33, sat)  # green hue
                time.sleep(0.15)

        results.append(run_test("Saturation sweep (green)", test_saturation_sweep))

        # Reset to default (Vector's standard teal)
        robot.behavior.set_eye_color(0.5, 1.0)

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"LEDs: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
