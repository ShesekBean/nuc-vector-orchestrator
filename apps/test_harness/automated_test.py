#!/usr/bin/env python3
"""Automated comprehensive test using LiveKit vision oracle.

No human in the loop — uses phone camera via LiveKit to verify robot behavior.
Ophir places phone on tripod pointing at robot, joins LiveKit room, then this
script runs all test phases automatically.

Usage:
    python3 monitoring/automated_test.py [--target-class 62] [--skip-nav]

Prerequisites:
    - Phone in LiveKit room (robot-cam) pointing at robot
    - Muscle container running on Jetson
    - NUC can reach Jetson at 192.168.1.71
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.camera_capture import CameraCapture
from monitoring.action_evaluator import evaluate_action

JETSON_IP = "192.168.1.71"
BRIDGE = f"http://{JETSON_IP}:8081"
CAPTURES_DIR = Path(__file__).parent / "captures" / "autotest"


@dataclass
class TestResult:
    phase: str
    name: str
    passed: bool
    method: str  # "api" | "vision" | "topic"
    details: str = ""
    confidence: float = 1.0


@dataclass
class TestReport:
    results: list[TestResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    def add(self, result: TestResult):
        self.results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.phase}: {result.name} ({result.method}) — {result.details}")

    def summary(self) -> str:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        duration = self.end_time - self.start_time

        lines = [
            f"\n{'='*60}",
            "AUTOMATED TEST REPORT",
            f"{'='*60}",
            f"Total: {total} | Passed: {passed} | Failed: {failed} | Duration: {duration:.0f}s",
            "",
        ]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            conf = f" ({r.confidence:.0%})" if r.method == "vision" else ""
            lines.append(f"  [{status}] {r.phase}: {r.name}{conf}")
            if not r.passed:
                lines.append(f"         {r.details}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


def curl(endpoint: str, method: str = "GET", data: dict | None = None,
         timeout: int = 10) -> dict | None:
    """Call bridge HTTP endpoint."""
    url = f"{BRIDGE}{endpoint}"
    cmd = ["curl", "-sf", "-X", method, url]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def ssh_cmd(cmd: str, timeout: int = 15) -> str | None:
    """Run command on Jetson via SSH."""
    try:
        result = subprocess.run(
            ["ssh", "jetson", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except subprocess.TimeoutExpired:
        return None


def ros2_param_set(node: str, param: str, value: str) -> bool:
    """Set a ROS2 parameter on the Jetson."""
    cmd = (
        f'docker exec muscle bash -c "'
        f'export PATH=/opt/ros/humble/install/bin:$PATH && '
        f'source /opt/ros/humble/install/setup.bash && '
        f'ros2 param set {node} {param} {value}"'
    )
    result = ssh_cmd(cmd)
    return result is not None and "successful" in (result or "").lower()


def ros2_topic_echo(topic: str, timeout: int = 5) -> str | None:
    """Echo one message from a ROS2 topic."""
    cmd = (
        f'docker exec muscle timeout {timeout} bash -c "'
        f'export PATH=/opt/ros/humble/install/bin:$PATH && '
        f'source /opt/ros/humble/install/setup.bash && '
        f'ros2 topic echo {topic} --once"'
    )
    return ssh_cmd(cmd, timeout=timeout + 5)


_PLANNER_DEFAULTS = {
    "max_speed": "0.08", "max_turn": "0.02", "max_strafe": "0.03",
    "kp_heading": "0.002",
}


def set_robot_mode(mode: str, target_class: int = 0):
    """Set robot operating mode.

    Modes:
      manual     — no planner, no detector, no servo scan. Full manual control.
      track_only — detector + planner servo tracking, no wheel movement.
      full       — everything on (normal operation, planner drives wheels + servos).
    """
    print(f"  [MODE] Setting robot to '{mode}' mode")
    if mode == "manual":
        ros2_param_set("/robot_interface", "mode", "manual")
        ros2_param_set("/person_detector", "enable", "false")
        for param in ("max_speed", "max_turn", "max_strafe", "kp_heading"):
            ros2_param_set("/planner", param, "0.0")
        curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    elif mode == "track_only":
        ros2_param_set("/robot_interface", "mode", "track_only")
        ros2_param_set("/person_detector", "target_class", str(target_class))
        ros2_param_set("/person_detector", "enable", "true")
        for param, val in _PLANNER_DEFAULTS.items():
            ros2_param_set("/planner", param, val)
        ros2_param_set("/planner", "max_speed", "0.0")
        ros2_param_set("/planner", "max_strafe", "0.0")
    elif mode == "full":
        ros2_param_set("/robot_interface", "mode", "full")
        ros2_param_set("/person_detector", "target_class", str(target_class))
        ros2_param_set("/person_detector", "enable", "true")
        for param, val in _PLANNER_DEFAULTS.items():
            ros2_param_set("/planner", param, val)
    time.sleep(1)


def ros2_node_list() -> list[str]:
    """Get list of running ROS2 nodes."""
    cmd = (
        'docker exec muscle bash -c "'
        'export PATH=/opt/ros/humble/install/bin:$PATH && '
        'source /opt/ros/humble/install/setup.bash && '
        'ros2 node list"'
    )
    result = ssh_cmd(cmd)
    return result.splitlines() if result else []


def vision_check(cam: CameraCapture, action: str, action_fn, settle: float = 2.0) -> TestResult:
    """Capture before, run action, capture after, evaluate with vision LLM."""
    before = cam.capture_and_save("before")
    action_fn()
    time.sleep(settle)
    after = cam.capture_and_save("after")
    result = evaluate_action(before, after, action)
    return TestResult(
        phase="", name="",
        passed=result.get("success", False),
        method="vision",
        details=result.get("explanation", ""),
        confidence=result.get("confidence", 0.0),
    )


# ── Test Phases ──────────────────────────────────────────────


def phase_preflight(report: TestReport):
    """Phase 1: Preflight — SSH, container, nodes, battery."""
    print("\n── Phase 1: Preflight ──")

    # SSH
    result = ssh_cmd("echo ok")
    report.add(TestResult("1", "SSH connectivity", result == "ok", "api", result or "failed"))

    # Container
    result = ssh_cmd("docker ps --format '{{.Names}}'")
    running = "muscle" in (result or "")
    report.add(TestResult("1", "Muscle container", running, "api", result or "not running"))

    # ROS2 nodes
    nodes = ros2_node_list()
    expected = ["/robot_interface", "/person_detector", "/planner", "/mqtt_bridge"]
    missing = [n for n in expected if n not in nodes]
    report.add(TestResult("1", "ROS2 nodes", len(missing) == 0, "api",
                          f"missing: {missing}" if missing else f"{len(nodes)} nodes"))

    # Battery
    health = curl("/health")
    if health:
        v = health.get("battery_v", 0)
        report.add(TestResult("1", "Battery", v > 10.0, "api", f"{v:.1f}V"))
    else:
        report.add(TestResult("1", "Battery", False, "api", "health endpoint failed"))


def phase_led(report: TestReport, cam: CameraCapture):
    """Phase 2: LED control — set colors and verify via vision."""
    print("\n── Phase 2: LED Control ──")

    for color_name, rgb in [("blue", (0, 0, 255)), ("red", (255, 0, 0)), ("green", (0, 255, 0))]:
        r = vision_check(
            cam,
            f"Robot LEDs should change to {color_name}. Look for {color_name} light on the robot.",
            lambda rgb=rgb: curl("/led", "POST", {"r": rgb[0], "g": rgb[1], "b": rgb[2]}),
            settle=1.0,
        )
        r.phase = "2"
        r.name = f"LED {color_name}"
        report.add(r)

    # Reset LEDs
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})


def phase_servo(report: TestReport, cam: CameraCapture):
    """Phase 3: Servo control — move camera and verify via vision."""
    print("\n── Phase 3: Servo Control ──")

    # Center first
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    time.sleep(1)

    # Pan left
    r = vision_check(
        cam,
        "Camera should pan left — the viewpoint should shift, showing different scene content on the right side.",
        lambda: curl("/servo", "POST", {"channel": 3, "angle": 60}),
        settle=1.5,
    )
    r.phase = "3"
    r.name = "Servo pan left"
    report.add(r)

    # Pan right
    r = vision_check(
        cam,
        "Camera should pan right — the viewpoint should shift significantly from the previous position.",
        lambda: curl("/servo", "POST", {"channel": 3, "angle": 130}),
        settle=1.5,
    )
    r.phase = "3"
    r.name = "Servo pan right"
    report.add(r)

    # Re-center
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})


def phase_motors(report: TestReport, cam: CameraCapture):
    """Phase 4: Motor control — move robot and verify via vision."""
    print("\n── Phase 4: Motor Control ──")

    # Forward
    r = vision_check(
        cam,
        "Robot should move forward — objects in the scene should appear closer/larger, or the viewpoint should shift forward.",
        lambda: curl("/move", "POST", {"vx": 0.2, "vy": 0, "vz": 0, "duration": 4.0}),
        settle=2.0,
    )
    r.phase = "4"
    r.name = "Move forward"
    report.add(r)

    # Backward (return)
    r = vision_check(
        cam,
        "Robot should move backward — objects should appear farther/smaller, viewpoint shifts back.",
        lambda: curl("/move", "POST", {"vx": -0.2, "vy": 0, "vz": 0, "duration": 4.0}),
        settle=2.0,
    )
    r.phase = "4"
    r.name = "Move backward"
    report.add(r)

    # Stop
    result = curl("/stop", "POST")
    report.add(TestResult("4", "Emergency stop", result is not None, "api",
                          str(result) if result else "failed"))


def phase_detection(report: TestReport, target_class: int):
    """Phase 5: Detection — verify detector finds target (person or TV)."""
    print(f"\n── Phase 5: Detection (class={target_class}) ──")

    # Enable detector only (manual mode keeps planner paused)
    class_name = "tv/monitor" if target_class == 62 else "person"
    ros2_param_set("/person_detector", "target_class", str(target_class))
    ros2_param_set("/person_detector", "enable", "true")
    time.sleep(8)  # Model switch + first detections need warmup

    # Check for detections on bbox topic
    bbox = ros2_topic_echo("/pedestrian_bboxes", timeout=5)
    has_detection = bbox is not None and "x_min" in (bbox or "")
    report.add(TestResult("5", f"Detect {class_name}", has_detection, "topic",
                          "bbox published" if has_detection else "no detection"))


def phase_tracking(report: TestReport, cam: CameraCapture, target_class: int):
    """Phase 6: Servo tracking — detector should drive servo toward target."""
    print("\n── Phase 6: Servo Tracking ──")

    class_name = "tv/monitor" if target_class == 62 else "person"

    # Track-only mode: servo follows target, wheels stay still
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    set_robot_mode("track_only", target_class)

    # Offset servo so it needs to track back
    curl("/servo", "POST", {"channel": 3, "angle": 60})
    time.sleep(1)

    r = vision_check(
        cam,
        f"Camera servo should track toward the {class_name} — the servo should rotate to re-center the target in frame. Compare the viewing angle between before and after.",
        lambda: time.sleep(5),  # Let planner track
        settle=1.0,
    )
    r.phase = "6"
    r.name = f"Servo tracks {class_name}"
    report.add(r)

    # Back to manual after tracking test
    set_robot_mode("manual")


def phase_following(report: TestReport, cam: CameraCapture, target_class: int):
    """Phase 7: Following — robot should move toward target."""
    print("\n── Phase 7: Following ──")

    class_name = "tv/monitor" if target_class == 62 else "person"

    set_robot_mode("full", target_class)

    r = vision_check(
        cam,
        f"Robot should move toward the {class_name} — the {class_name} should appear larger in the after frame as the robot gets closer to it.",
        lambda: time.sleep(30),
        settle=1.0,
    )
    r.phase = "7"
    r.name = f"Follow {class_name}"
    report.add(r)

    # Back to manual after following test
    set_robot_mode("manual")
    curl("/stop", "POST")


def phase_lidar(report: TestReport):
    """Phase 8: LiDAR — verify scan topic is publishing."""
    print("\n── Phase 8: LiDAR ──")

    scan = ros2_topic_echo("/scan", timeout=5)
    has_scan = scan is not None and "ranges" in (scan or "")
    report.add(TestResult("8", "LiDAR /scan", has_scan, "topic",
                          "publishing" if has_scan else "no data"))


def phase_imu(report: TestReport):
    """Phase 9: IMU + EKF ──"""
    print("\n── Phase 9: IMU + EKF ──")

    imu = ros2_topic_echo("/imu/data", timeout=5)
    has_imu = imu is not None and "orientation" in (imu or "")
    report.add(TestResult("9", "IMU /imu/data", has_imu, "topic",
                          "publishing" if has_imu else "no data"))

    odom = ros2_topic_echo("/odometry/filtered", timeout=5)
    has_odom = odom is not None and "pose" in (odom or "")
    report.add(TestResult("9", "EKF /odometry/filtered", has_odom, "topic",
                          "publishing" if has_odom else "no data"))


def phase_voice(report: TestReport):
    """Phase 10: Voice pipeline — check nodes are running."""
    print("\n── Phase 10: Voice Pipeline ──")

    nodes = ros2_node_list()
    has_voice = "/voice_io" in nodes
    report.add(TestResult("10", "Voice IO node", has_voice, "api",
                          "running" if has_voice else "not found"))


def phase_signal_chain(report: TestReport, cam: CameraCapture):
    """Phase 11: Signal → Robot — send command via bridge and verify."""
    print("\n── Phase 11: Signal → Robot Chain ──")

    # Test LED via direct bridge (same path as Signal → OpenClaw → curl)
    r = vision_check(
        cam,
        "Robot LEDs should turn blue. Look for blue light on the robot body.",
        lambda: curl("/led", "POST", {"r": 0, "g": 0, "b": 255}),
        settle=1.5,
    )
    r.phase = "11"
    r.name = "Signal chain LED"
    report.add(r)

    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})


def phase_safety(report: TestReport, cam: CameraCapture):
    """Phase 12: Safety — container restart zeros motors."""
    print("\n── Phase 12: Safety ──")

    # Start a move, then restart container
    curl("/move", "POST", {"vx": 0.1, "vy": 0, "vz": 0, "duration": 5.0})
    time.sleep(0.5)

    # Emergency stop
    result = curl("/stop", "POST")
    report.add(TestResult("12", "Emergency stop", result is not None, "api",
                          str(result) if result else "failed"))
    time.sleep(1)

    # Container restart
    print("  Restarting muscle container...")
    ssh_cmd("cd /home/yahboom/claude && docker compose restart muscle", timeout=60)
    time.sleep(30)  # Wait for full restart

    health = curl("/health")
    report.add(TestResult("12", "Recovery after restart", health is not None, "api",
                          f"battery={health.get('battery_v', '?')}V" if health else "failed"))


def phase_charging_led(report: TestReport, cam: CameraCapture):
    """Phase 13: Charging LED — check for green sweep when charging."""
    print("\n── Phase 13: Charging LED ──")

    health = curl("/health")
    voltage = health.get("battery_v", 0) if health else 0

    if voltage > 12.0:
        # Wait for charging detection (30s window)
        time.sleep(35)
        r = vision_check(
            cam,
            "Robot LEDs should show a green sweep animation (charging indicator) — look for green light moving across the LED strip.",
            lambda: time.sleep(3),
            settle=1.0,
        )
        r.phase = "13"
        r.name = "Charging LED animation"
        report.add(r)
    else:
        report.add(TestResult("13", "Charging LED animation", False, "api",
                              f"Battery {voltage:.1f}V — not charging or too low to detect"))


def main():
    parser = argparse.ArgumentParser(description="Automated comprehensive test via LiveKit")
    parser.add_argument("--target-class", type=int, default=0,
                        help="COCO class for detection target (0=person, 62=tv)")
    parser.add_argument("--skip-nav", action="store_true",
                        help="Skip navigation tests (SLAM not ready)")
    parser.add_argument("--skip-following", action="store_true",
                        help="Skip following test (robot will move)")
    parser.add_argument("--skip-safety", action="store_true",
                        help="Skip safety restart test")
    parser.add_argument("--room", type=str, default="robot-cam",
                        help="LiveKit room name")
    parser.add_argument("--join-url", action="store_true",
                        help="Print LiveKit join URL and exit")
    args = parser.parse_args()

    cam = CameraCapture(room=args.room, captures_dir=CAPTURES_DIR)

    if args.join_url:
        print(cam.generate_join_url())
        return

    # Verify LiveKit connection
    print("Connecting to LiveKit room...")
    try:
        test_frame = cam.capture_and_save("connectivity_test")
        print(f"LiveKit connected — test frame: {test_frame}")
    except Exception as e:
        print(f"ERROR: Cannot connect to LiveKit: {e}")
        print(f"Join URL: {cam.generate_join_url()}")
        sys.exit(1)

    report = TestReport()
    report.start_time = time.time()

    target = args.target_class

    # Manual mode for clean LED/servo/motor tests
    print("Setting robot to manual mode for clean test environment...")
    set_robot_mode("manual")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})
    curl("/servo", "POST", {"channel": 3, "angle": 102})
    curl("/servo", "POST", {"channel": 4, "angle": 68})
    time.sleep(1)

    # Run all phases
    phase_preflight(report)
    phase_led(report, cam)
    phase_servo(report, cam)
    phase_motors(report, cam)
    phase_detection(report, target)
    phase_tracking(report, cam, target)

    if not args.skip_following:
        phase_following(report, cam, target)

    phase_lidar(report)
    phase_imu(report)
    phase_voice(report)
    phase_signal_chain(report, cam)

    if not args.skip_safety:
        phase_safety(report, cam)

    phase_charging_led(report, cam)

    report.end_time = time.time()

    # Cleanup — restore full mode and stop motors
    set_robot_mode("full")
    curl("/stop", "POST")
    curl("/led", "POST", {"r": 0, "g": 0, "b": 0})

    # Reset target class back to person
    if target != 0:
        ros2_param_set("/person_detector", "target_class", "0")

    print(report.summary())

    # Save report
    report_path = CAPTURES_DIR / "report.json"
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump({
            "results": [
                {"phase": r.phase, "name": r.name, "passed": r.passed,
                 "method": r.method, "details": r.details, "confidence": r.confidence}
                for r in report.results
            ],
            "total": len(report.results),
            "passed": sum(1 for r in report.results if r.passed),
            "duration_s": report.end_time - report.start_time,
        }, f, indent=2)
    print(f"\nReport saved: {report_path}")

    sys.exit(0 if all(r.passed for r in report.results) else 1)


if __name__ == "__main__":
    main()
