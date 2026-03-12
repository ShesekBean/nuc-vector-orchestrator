#!/usr/bin/env python3
"""Standalone sensor test — read battery, gyro, accel, touch, proximity, pose.

Run: python3 apps/vector/tests/standalone/test_sensors.py
"""

import sys
import time

import anki_vector

SERIAL = "0dd1cdcf"
PASS = 0
FAIL = 1


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
    print("STANDALONE TEST: Sensors")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []

    try:
        robot.connect()
        print("Connected to Vector\n")

        # Sub-test 1: Battery state
        def test_battery():
            batt = robot.get_battery_state()
            volts = batt.battery_volts
            level = batt.battery_level
            charging = batt.is_charging
            if volts <= 0:
                raise RuntimeError(f"Invalid voltage: {volts}")
            return f"{volts:.2f}V, level={level}, charging={charging}"

        results.append(run_test("Battery state", test_battery))

        # Sub-test 2: Accelerometer
        def test_accel():
            accel = robot.accel
            if accel is None:
                raise RuntimeError("Accelerometer returned None")
            return f"x={accel.x:.2f} y={accel.y:.2f} z={accel.z:.2f}"

        results.append(run_test("Accelerometer", test_accel))

        # Sub-test 3: Gyroscope
        def test_gyro():
            gyro = robot.gyro
            if gyro is None:
                raise RuntimeError("Gyroscope returned None")
            return f"x={gyro.x:.2f} y={gyro.y:.2f} z={gyro.z:.2f}"

        results.append(run_test("Gyroscope", test_gyro))

        # Sub-test 4: Head angle
        def test_head_angle():
            angle = robot.head_angle_rad
            if angle is None:
                raise RuntimeError("Head angle returned None")
            import math
            deg = math.degrees(angle)
            return f"{deg:.1f}°"

        results.append(run_test("Head angle", test_head_angle))

        # Sub-test 5: Lift height
        def test_lift_height():
            height = robot.lift_height_mm
            if height is None:
                raise RuntimeError("Lift height returned None")
            return f"{height:.1f}mm"

        results.append(run_test("Lift height", test_lift_height))

        # Sub-test 6: Pose (position + heading)
        def test_pose():
            pose = robot.pose
            if pose is None:
                raise RuntimeError("Pose returned None")
            return f"x={pose.x:.1f} y={pose.y:.1f} z={pose.z:.1f}"

        results.append(run_test("Pose (position)", test_pose))

        # Sub-test 7: Touch sensor
        def test_touch():
            touch = robot.touch
            return f"touched={touch.is_being_touched if hasattr(touch, 'is_being_touched') else touch}"

        results.append(run_test("Touch sensor", test_touch))

        # Sub-test 8: Proximity / cliff
        def test_proximity():
            prox = robot.proximity
            if prox is None:
                raise RuntimeError("Proximity returned None")
            dist = prox.distance
            return f"distance={dist.distance_mm:.0f}mm" if hasattr(dist, "distance_mm") else f"raw={dist}"

        results.append(run_test("Proximity sensor", test_proximity))

        # Sub-test 9: Wheel speeds (should be 0 when stationary)
        def test_wheels():
            left = robot.left_wheel_speed_mmps
            right = robot.right_wheel_speed_mmps
            return f"left={left:.1f} right={right:.1f} mm/s"

        results.append(run_test("Wheel speeds", test_wheels))

        # Sub-test 10: Version info
        def test_version():
            ver = robot.get_version_state()
            return f"os={ver.os_version}"

        results.append(run_test("Version state", test_version))

    finally:
        robot.disconnect()

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Sensors: {passed}/{total} passed")
    print("=" * 60)
    return PASS if all(results) else FAIL


if __name__ == "__main__":
    sys.exit(main())
