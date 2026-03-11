#!/usr/bin/env python3
"""Vector hardware health check — tests all 7 subsystems via SDK.

Connects to Vector over WiFi, exercises each hardware subsystem,
and reports pass/fail with latency per call. Suitable for systemd
health checks (exit 0 = all pass, exit 1 = any failure).

Run: python3 -m apps.vector.bridge.health_check
"""

import sys
import time

SERIAL = "0dd1cdcf"


class SubsystemResult:
    """Result of a single subsystem health check."""

    __slots__ = ("name", "passed", "latency_ms", "detail")

    def __init__(self, name: str, passed: bool, latency_ms: float, detail: str):
        self.name = name
        self.passed = passed
        self.latency_ms = latency_ms
        self.detail = detail


def _check_subsystem(name: str, fn) -> SubsystemResult:
    """Run a subsystem check with timing."""
    t0 = time.monotonic()
    try:
        detail = fn() or "OK"
        elapsed_ms = (time.monotonic() - t0) * 1000
        return SubsystemResult(name, True, elapsed_ms, detail)
    except Exception as e:
        elapsed_ms = (time.monotonic() - t0) * 1000
        return SubsystemResult(name, False, elapsed_ms, str(e))


def check_motors(robot) -> SubsystemResult:
    """Brief motor pulse — wheels forward then stop."""
    def fn():
        robot.motors.set_wheel_motors(50, 50)
        time.sleep(0.1)
        robot.motors.set_wheel_motors(0, 0)
        return "pulse 50mm/s for 0.1s"
    return _check_subsystem("motors", fn)


def check_leds(robot) -> SubsystemResult:
    """Cycle eye color through red, green, blue then reset."""
    def fn():
        for hue in (0.0, 0.33, 0.67):
            robot.behavior.set_eye_color(hue, 1.0)
            time.sleep(0.15)
        robot.behavior.set_eye_color(0.5, 1.0)  # reset to teal
        return "cycled R/G/B, reset to teal"
    return _check_subsystem("leds", fn)


def check_camera(robot) -> SubsystemResult:
    """Capture a single frame and verify dimensions."""
    def fn():
        image = robot.camera.capture_single_image()
        if image is None:
            raise RuntimeError("capture returned None")
        w, h = image.raw_image.size
        if w < 320 or h < 180:
            raise RuntimeError(f"image too small: {w}x{h}")
        return f"{w}x{h}"
    return _check_subsystem("camera", fn)


def check_head(robot) -> SubsystemResult:
    """Move head to neutral (0 degrees)."""
    def fn():
        from anki_vector.util import degrees
        robot.behavior.set_head_angle(degrees(0))
        return "set to 0°"
    return _check_subsystem("head", fn)


def check_lift(robot) -> SubsystemResult:
    """Move lift to stowed position (0.0)."""
    def fn():
        robot.behavior.set_lift_height(0.0)
        return "set to 0.0 (stowed)"
    return _check_subsystem("lift", fn)


def check_battery(robot) -> SubsystemResult:
    """Read battery state."""
    def fn():
        batt = robot.get_battery_state()
        voltage = batt.battery_volts
        level = batt.battery_level
        return f"{voltage:.2f}V level={level}"
    return _check_subsystem("battery", fn)


def check_sensors(robot) -> SubsystemResult:
    """Read robot state — cliff sensors, touch, accelerometer."""
    def fn():
        # RobotState is available via the robot's properties after connecting
        accel = robot.accel
        gyro = robot.gyro
        touch = robot.touch.last_sensor_reading
        if accel is None:
            raise RuntimeError("accelerometer data unavailable")
        return (
            f"accel=({accel.x:.1f},{accel.y:.1f},{accel.z:.1f}) "
            f"gyro=({gyro.x:.1f},{gyro.y:.1f},{gyro.z:.1f}) "
            f"touch={touch}"
        )
    return _check_subsystem("sensors", fn)


ALL_CHECKS = [
    check_motors,
    check_leds,
    check_camera,
    check_head,
    check_lift,
    check_battery,
    check_sensors,
]


def run_health_check(serial: str = SERIAL) -> list[SubsystemResult]:
    """Connect to Vector and run all subsystem checks.

    Returns list of SubsystemResult. Caller decides what to do with results.
    """
    import anki_vector
    robot = anki_vector.Robot(serial=serial, default_logging=False)
    results = []

    try:
        robot.connect()
        for check_fn in ALL_CHECKS:
            results.append(check_fn(robot))
    except Exception as e:
        # Connection-level failure — mark remaining checks as failed
        if not results:
            results.append(SubsystemResult("connection", False, 0.0, str(e)))
        else:
            results.append(SubsystemResult("connection_lost", False, 0.0, str(e)))
    finally:
        try:
            robot.disconnect()
        except Exception:
            pass

    return results


def format_results(results: list[SubsystemResult]) -> str:
    """Format results as clean CLI output."""
    lines = []
    lines.append("=" * 60)
    lines.append("VECTOR HEALTH CHECK")
    lines.append("=" * 60)

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"  [{r.name:18s}] {status}  {r.latency_ms:7.1f}ms  {r.detail}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    lines.append("=" * 60)
    lines.append(f"Result: {passed}/{total} passed")

    if passed == total:
        lines.append("Status: HEALTHY")
    else:
        failed = [r.name for r in results if not r.passed]
        lines.append(f"Status: UNHEALTHY — failed: {', '.join(failed)}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> int:
    """Entry point — run health check and print results."""
    results = run_health_check()
    print(format_results(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
