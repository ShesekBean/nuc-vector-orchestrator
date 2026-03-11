#!/usr/bin/env python3
"""Standalone motor test — exercises wheels, drive, and turn.

WARNING: This test MOVES the robot physically. Ensure clear space around Vector.

Run: python3 apps/vector/tests/standalone/test_motors.py
"""

import sys
import time

import anki_vector
from anki_vector.util import degrees, distance_mm, speed_mmps

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1


def run_test(name, fn):
    """Run a single sub-test with timing and pass/fail output."""
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
    print("STANDALONE TEST: Motors")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print(f"Connected to {robot.name}\n")

        # Sub-test 1: Drive forward
        def test_drive_forward():
            robot.behavior.drive_straight(distance_mm(50), speed_mmps(50))

        results.append(run_test("Drive forward 50mm", test_drive_forward))

        time.sleep(0.5)

        # Sub-test 2: Drive backward
        def test_drive_backward():
            robot.behavior.drive_straight(distance_mm(-50), speed_mmps(50))

        results.append(run_test("Drive backward 50mm", test_drive_backward))

        time.sleep(0.5)

        # Sub-test 3: Turn right 90°
        def test_turn_right():
            robot.behavior.turn_in_place(degrees(-90))

        results.append(run_test("Turn right 90°", test_turn_right))

        time.sleep(0.5)

        # Sub-test 4: Turn left 90° (back to original heading)
        def test_turn_left():
            robot.behavior.turn_in_place(degrees(90))

        results.append(run_test("Turn left 90°", test_turn_left))

        time.sleep(0.5)

        # Sub-test 5: Raw wheel motors — speed ramp
        def test_speed_ramp():
            for speed in [50, 100, 150, 100, 50, 0]:
                robot.motors.set_wheel_motors(speed, speed)
                time.sleep(0.3)
            robot.motors.set_wheel_motors(0, 0)

        results.append(run_test("Speed ramp (50→150→0)", test_speed_ramp))

        # Sub-test 6: Spin in place via raw wheels
        def test_spin():
            robot.motors.set_wheel_motors(80, -80)
            time.sleep(0.5)
            robot.motors.set_wheel_motors(0, 0)

        results.append(run_test("Spin via raw wheels", test_spin))

    finally:
        robot.motors.stop_all_motors()
        robot.disconnect()

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Motors: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
