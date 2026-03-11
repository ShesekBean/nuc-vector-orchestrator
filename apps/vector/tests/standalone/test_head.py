#!/usr/bin/env python3
"""Standalone head servo test — sweep full range, neutral, speed control.

Run: python3 apps/vector/tests/standalone/test_head.py
"""

import sys
import time

import anki_vector
from anki_vector.util import degrees

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1

# Head range: -22° (full down) to +45° (full up)
HEAD_MIN = -22.0
HEAD_MAX = 45.0
HEAD_NEUTRAL = 10.0


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
    print("STANDALONE TEST: Head Servo")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print(f"Connected to {robot.name}\n")

        # Sub-test 1: Move to neutral
        def test_neutral():
            robot.behavior.set_head_angle(degrees(HEAD_NEUTRAL))
            time.sleep(0.3)

        results.append(run_test("Head to neutral (10°)", test_neutral))

        # Sub-test 2: Full down
        def test_full_down():
            robot.behavior.set_head_angle(degrees(HEAD_MIN))
            time.sleep(0.5)

        results.append(run_test(f"Head full down ({HEAD_MIN}°)", test_full_down))

        # Sub-test 3: Full up
        def test_full_up():
            robot.behavior.set_head_angle(degrees(HEAD_MAX))
            time.sleep(0.5)

        results.append(run_test(f"Head full up ({HEAD_MAX}°)", test_full_up))

        # Sub-test 4: Sweep down → up in steps
        def test_sweep():
            for angle in range(int(HEAD_MIN), int(HEAD_MAX) + 1, 10):
                robot.behavior.set_head_angle(degrees(angle))
                time.sleep(0.2)

        results.append(run_test("Sweep full range (10° steps)", test_sweep))

        # Sub-test 5: Fast move (high speed param)
        def test_fast():
            robot.behavior.set_head_angle(degrees(HEAD_MIN), max_speed=10.0)
            time.sleep(0.3)
            robot.behavior.set_head_angle(degrees(HEAD_MAX), max_speed=10.0)
            time.sleep(0.3)

        results.append(run_test("Fast move (max_speed=10)", test_fast))

        # Sub-test 6: Slow move (low speed param)
        def test_slow():
            robot.behavior.set_head_angle(degrees(HEAD_MIN), max_speed=1.0)
            time.sleep(1.0)
            robot.behavior.set_head_angle(degrees(HEAD_NEUTRAL), max_speed=1.0)
            time.sleep(1.0)

        results.append(run_test("Slow move (max_speed=1)", test_slow))

        # Return to neutral
        robot.behavior.set_head_angle(degrees(HEAD_NEUTRAL))

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Head: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
