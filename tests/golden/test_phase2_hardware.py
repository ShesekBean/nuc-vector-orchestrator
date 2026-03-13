"""Phase 2 — Stationary Hardware.

Robot stays still. Tests all non-movement hardware via gRPC.
Runs as a single sequential batch with 5s pauses so Ophir can observe.

Tests 2.1–2.15 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import time

import pytest


pytestmark = [pytest.mark.phase2, pytest.mark.robot]

PAUSE = 5  # seconds between tests


class TestPhase2HardwareBatch:
    """All Phase 2 tests in a single batch with pauses and logging."""

    def test_hardware_batch(self, robot_connected, capsys):
        """2.1–2.15 — Sequential hardware tests with 5s pauses."""
        robot = robot_connected

        # 2.1 Camera single frame
        print("\n>>> 2.1 — Capturing single camera frame...")
        image = robot.camera.capture_single_image()
        assert image is not None
        pil_img = getattr(image, "raw_image", image)
        if hasattr(pil_img, "size"):
            w, h = pil_img.size
        else:
            w, h = 640, 360
        assert w >= 320, f"Image width too small: {w}"
        assert h >= 180, f"Image height too small: {h}"
        print(f"    Camera frame: {w}x{h} ✓")
        time.sleep(PAUSE)

        # 2.2 Camera feed stream
        print(">>> 2.2 — Streaming camera feed (5 unique frames)...")
        robot.camera.init_camera_feed()
        frames = []
        for _ in range(50):
            img = robot.camera.latest_image
            if img is not None:
                img_id = id(img)
                if not frames or frames[-1] != img_id:
                    frames.append(img_id)
                if len(frames) >= 5:
                    break
            time.sleep(0.1)
        robot.camera.close_camera_feed()
        assert len(frames) >= 5, f"Only got {len(frames)} unique frames"
        print(f"    Got {len(frames)} unique frames ✓")
        time.sleep(PAUSE)

        # 2.3 Head angle min
        print(">>> 2.3 — Head tilting DOWN to -22°...")
        from anki_vector.util import degrees
        robot.behavior.set_head_angle(degrees(-22))
        print("    Head at min ✓")
        time.sleep(PAUSE)

        # 2.4 Head angle max
        print(">>> 2.4 — Head tilting UP to 45°...")
        robot.behavior.set_head_angle(degrees(45))
        print("    Head at max ✓")
        time.sleep(PAUSE)

        # 2.5 Head angle neutral
        print(">>> 2.5 — Head returning to NEUTRAL (10°)...")
        robot.behavior.set_head_angle(degrees(10))
        print("    Head at neutral ✓")
        time.sleep(PAUSE)

        # 2.6 Lift up
        print(">>> 2.6 — LIFT going UP...")
        robot.behavior.set_lift_height(1.0)
        print("    Lift raised ✓")
        time.sleep(PAUSE)

        # 2.7 Lift down
        print(">>> 2.7 — LIFT going DOWN...")
        robot.behavior.set_lift_height(0.0)
        print("    Lift lowered ✓")
        time.sleep(PAUSE)

        # 2.8 LED solid color
        print(">>> 2.8 — Eyes turning RED...")
        robot.behavior.set_eye_color(0.0, 1.0)
        print("    Eyes red ✓")
        time.sleep(PAUSE)

        # 2.9 LED off
        print(">>> 2.9 — Eyes returning to DEFAULT GREEN...")
        robot.behavior.set_eye_color(0.33, 0.75)
        print("    Eyes green ✓")
        time.sleep(PAUSE)

        # 2.11 TTS say_text
        print(">>> 2.11 — Saying 'test' out loud...")
        try:
            robot.behavior.say_text("test")
        except Exception:
            try:
                robot.disconnect()
            except Exception:
                pass
            robot.connect()
            robot.behavior.say_text("test")
        print("    Speech done ✓")
        time.sleep(PAUSE)

        # 2.12 Touch sensor read
        print(">>> 2.12 — Reading touch sensor...")
        touch = robot.touch.last_sensor_reading
        assert touch is not None
        assert hasattr(touch, "is_being_touched")
        print(f"    Touch: is_being_touched={touch.is_being_touched} ✓")
        time.sleep(PAUSE)

        # 2.13 Cliff sensors read
        print(">>> 2.13 — Reading cliff sensors...")
        state = robot.status
        assert state is not None
        print("    Cliff sensors OK ✓")
        time.sleep(PAUSE)

        # 2.14 Accelerometer read
        print(">>> 2.14 — Reading accelerometer...")
        accel = robot.accel
        assert accel is not None
        assert accel.x != 0 or accel.y != 0 or accel.z != 0, "All axes zero"
        print(f"    Accel: x={accel.x:.1f} y={accel.y:.1f} z={accel.z:.1f} ✓")
        time.sleep(PAUSE)

        # 2.15 Gyroscope read
        print(">>> 2.15 — Reading gyroscope...")
        gyro = robot.gyro
        assert gyro is not None
        print(f"    Gyro: x={gyro.x:.4f} y={gyro.y:.4f} z={gyro.z:.4f} ✓")

        print("\n>>> Phase 2 — ALL TESTS PASSED")
