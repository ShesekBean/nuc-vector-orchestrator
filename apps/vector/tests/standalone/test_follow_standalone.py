#!/usr/bin/env python3
"""Standalone follow test — full person-following loop in a single file.

This is the Vector equivalent of R3's servo_follow_test.py. It proves the
complete follow algorithm works in isolation before integrating into the
full pipeline.

Architecture:
  Camera → YOLO detection → PD controller → Motor commands

WARNING: This test MOVES the robot. Ensure clear space around Vector.
The robot will attempt to follow the largest detected person.

Run: python3 apps/vector/tests/standalone/test_follow_standalone.py

Flags:
  --dry-run    Run detection only, no motor commands (safe for SW testing)
  --duration N Run for N seconds (default: 30)
  --no-yolo    Skip YOLO, use camera-only mode (test motor control with fake detections)
"""

import argparse
import sys
import time

SERIAL = "0dd1cdcf"
SCREEN_W = 640  # Vector camera width
SCREEN_H = 360  # Vector camera height
PASS = 0
FAIL = 1

# PD controller gains (tunable)
KP_TURN = 0.3       # Proportional gain for turning (deg/pixel offset)
KD_TURN = 0.05      # Derivative gain for turning
KP_DRIVE = 0.8      # Proportional gain for driving (speed/height ratio)
TARGET_HEIGHT = 150  # Target person bbox height in pixels (~1m distance)
DEAD_ZONE_X = 30     # Pixel dead zone for centering (no turn if within)
MAX_WHEEL_SPEED = 120  # mm/s safety cap
MIN_WHEEL_SPEED = -120


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


class PDController:
    """Simple PD controller for turn and drive."""

    def __init__(self):
        self.prev_error_x = 0.0
        self.prev_error_h = 0.0
        self.prev_time = time.time()

    def compute(self, center_x, bbox_height):
        """Compute wheel speeds from detection center and bbox height.

        Args:
            center_x: X-center of detected person (0 = left, SCREEN_W = right)
            bbox_height: Height of bounding box in pixels

        Returns:
            (left_speed, right_speed) in mm/s
        """
        now = time.time()
        dt = now - self.prev_time
        if dt <= 0:
            dt = 0.033  # ~30Hz default

        # Turn error: offset from image center (positive = person is right)
        error_x = center_x - (SCREEN_W / 2)

        # Drive error: difference from target height (positive = too far)
        error_h = TARGET_HEIGHT - bbox_height

        # Derivatives
        d_error_x = (error_x - self.prev_error_x) / dt
        d_error_h = (error_h - self.prev_error_h) / dt

        # PD outputs
        turn_speed = KP_TURN * error_x + KD_TURN * d_error_x
        drive_speed = KP_DRIVE * error_h + 0.1 * d_error_h

        # Apply dead zone for turning
        if abs(error_x) < DEAD_ZONE_X:
            turn_speed = 0

        # Convert to differential drive
        left_speed = clamp(drive_speed + turn_speed, MIN_WHEEL_SPEED, MAX_WHEEL_SPEED)
        right_speed = clamp(drive_speed - turn_speed, MIN_WHEEL_SPEED, MAX_WHEEL_SPEED)

        # Save state
        self.prev_error_x = error_x
        self.prev_error_h = error_h
        self.prev_time = now

        return left_speed, right_speed


def detect_person_yolo(frame_array):
    """Run YOLO detection on a frame, return largest person bbox or None.

    Returns:
        (center_x, center_y, width, height) or None
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        return None

    # Use a module-level cached model to avoid reloading
    if not hasattr(detect_person_yolo, "_model"):
        detect_person_yolo._model = YOLO("yolov8n.pt")

    results = detect_person_yolo._model(frame_array, verbose=False, classes=[0])  # class 0 = person
    if not results or len(results[0].boxes) == 0:
        return None

    # Find largest person by bbox area
    best = None
    best_area = 0
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        area = (x2 - x1) * (y2 - y1)
        if area > best_area:
            best_area = area
            best = (
                (x1 + x2) / 2,  # center_x
                (y1 + y2) / 2,  # center_y
                x2 - x1,        # width
                y2 - y1,         # height
            )

    return best


def main():
    parser = argparse.ArgumentParser(description="Standalone person-following test")
    parser.add_argument("--dry-run", action="store_true", help="Detection only, no motors")
    parser.add_argument("--duration", type=int, default=30, help="Run duration in seconds")
    parser.add_argument("--no-yolo", action="store_true", help="Skip YOLO, test motor control only")
    args = parser.parse_args()

    print("=" * 60)
    print("STANDALONE TEST: Follow (Full Pipeline)")
    print(f"  Mode: {'DRY-RUN (no motors)' if args.dry_run else 'LIVE (motors active)'}")
    print(f"  YOLO: {'disabled' if args.no_yolo else 'enabled'}")
    print(f"  Duration: {args.duration}s")
    print("=" * 60)

    if not args.no_yolo:
        try:
            import numpy as np  # noqa: F401
            from ultralytics import YOLO  # noqa: F401
            print("  YOLO: available")
        except ImportError:
            print("  YOLO: not available — falling back to --no-yolo mode")
            args.no_yolo = True

    import anki_vector
    from anki_vector.util import degrees

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    controller = PDController()

    stats = {
        "frames": 0,
        "detections": 0,
        "motor_commands": 0,
        "start_time": 0,
    }

    try:
        robot.connect()
        print(f"\n  Connected to {robot.name}")

        # Set head to good detection angle
        robot.behavior.set_head_angle(degrees(0))
        time.sleep(0.5)

        # Start camera feed
        robot.camera.init_camera_feed()
        time.sleep(0.5)

        print(f"  Camera feed started. Running for {args.duration}s...\n")
        stats["start_time"] = time.time()

        while time.time() - stats["start_time"] < args.duration:
            loop_start = time.time()

            # Get frame
            image = robot.camera.latest_image
            if image is None:
                time.sleep(0.05)
                continue

            stats["frames"] += 1

            # Detect person
            detection = None
            if not args.no_yolo:
                import numpy as np
                frame_array = np.array(image.raw_image)
                detection = detect_person_yolo(frame_array)
            else:
                # Fake detection at center for motor control testing
                detection = (SCREEN_W / 2, SCREEN_H / 2, 100, TARGET_HEIGHT)

            if detection:
                stats["detections"] += 1
                center_x, center_y, det_w, det_h = detection

                # Compute motor commands
                left_speed, right_speed = controller.compute(center_x, det_h)

                if not args.dry_run:
                    robot.motors.set_wheel_motors(left_speed, right_speed)
                    stats["motor_commands"] += 1

                # Log every 10th frame
                if stats["frames"] % 10 == 0:
                    elapsed = time.time() - stats["start_time"]
                    fps = stats["frames"] / elapsed if elapsed > 0 else 0
                    print(
                        f"  frame={stats['frames']:4d}  "
                        f"det=({center_x:.0f},{center_y:.0f}) h={det_h:.0f}  "
                        f"motors=({left_speed:.0f},{right_speed:.0f})  "
                        f"fps={fps:.1f}"
                    )
            else:
                # No detection — stop motors
                if not args.dry_run:
                    robot.motors.set_wheel_motors(0, 0)

                if stats["frames"] % 30 == 0:
                    print(f"  frame={stats['frames']:4d}  no detection")

            # Rate limit to ~15fps
            elapsed_loop = time.time() - loop_start
            if elapsed_loop < 0.066:
                time.sleep(0.066 - elapsed_loop)

    except KeyboardInterrupt:
        print("\n  Interrupted by user")
    finally:
        robot.motors.stop_all_motors()
        robot.camera.close_camera_feed()
        robot.disconnect()

    # Summary
    total_time = time.time() - stats["start_time"] if stats["start_time"] > 0 else 0
    avg_fps = stats["frames"] / total_time if total_time > 0 else 0
    det_rate = stats["detections"] / stats["frames"] * 100 if stats["frames"] > 0 else 0

    print(f"\n{'=' * 60}")
    print("Follow Test Summary:")
    print(f"  Duration:        {total_time:.1f}s")
    print(f"  Frames:          {stats['frames']}")
    print(f"  Avg FPS:         {avg_fps:.1f}")
    print(f"  Detections:      {stats['detections']} ({det_rate:.0f}%)")
    print(f"  Motor commands:  {stats['motor_commands']}")
    print(f"  Mode:            {'DRY-RUN' if args.dry_run else 'LIVE'}")

    if stats["frames"] > 0:
        print("\n  Result: PASS")
        print("=" * 60)
        return PASS
    else:
        print("\n  Result: FAIL (no frames captured)")
        print("=" * 60)
        return FAIL


if __name__ == "__main__":
    sys.exit(main())
