#!/usr/bin/env python3
"""Standalone lift test — full range sweep and presets.

Run: python3 apps/vector/tests/standalone/test_lift.py
"""

import sys
import time

import anki_vector

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1

# Lift range: 0.0 (full down) to 1.0 (full up)
PRESETS = {
    "down": 0.0,
    "carry": 0.5,
    "high": 0.8,
    "full_up": 1.0,
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
    print("STANDALONE TEST: Lift")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print("Connected to Vector\n")

        # Sub-test 1: Full down
        def test_full_down():
            robot.behavior.set_lift_height(0.0)
            time.sleep(0.5)

        results.append(run_test("Lift full down (0.0)", test_full_down))

        # Sub-test 2: Full up
        def test_full_up():
            robot.behavior.set_lift_height(1.0)
            time.sleep(0.5)

        results.append(run_test("Lift full up (1.0)", test_full_up))

        # Sub-test 3: Presets
        for name, height in PRESETS.items():
            def test_preset(h=height):
                robot.behavior.set_lift_height(h)
                time.sleep(0.4)

            results.append(run_test(f"Preset '{name}' ({height})", test_preset))

        # Sub-test 4: Sweep up in steps
        def test_sweep_up():
            for h in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
                robot.behavior.set_lift_height(h)
                time.sleep(0.3)

        results.append(run_test("Sweep up (0.2 steps)", test_sweep_up))

        # Sub-test 5: Sweep down in steps
        def test_sweep_down():
            for h in [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]:
                robot.behavior.set_lift_height(h)
                time.sleep(0.3)

        results.append(run_test("Sweep down (0.2 steps)", test_sweep_down))

        # Sub-test 6: Fast move
        def test_fast():
            robot.behavior.set_lift_height(1.0, max_speed=10.0)
            time.sleep(0.3)
            robot.behavior.set_lift_height(0.0, max_speed=10.0)
            time.sleep(0.3)

        results.append(run_test("Fast move (max_speed=10)", test_fast))

        # Return to down
        robot.behavior.set_lift_height(0.0)

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Lift: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
