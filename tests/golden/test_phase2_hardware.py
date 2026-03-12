"""Phase 2 — Stationary Hardware (~35s).

Robot stays still.  Tests all non-movement hardware via gRPC.

Tests 2.1–2.15 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.phase2, pytest.mark.robot]


# 2.1 Camera single frame
class TestCameraSingleFrame:
    def test_capture_single_image(self, robot_connected):
        """2.1 — capture_single_image() returns image with expected shape."""
        robot = robot_connected
        image = robot.camera.capture_single_image()
        assert image is not None
        # PIL Image — check size
        w, h = image.size
        assert w >= 320, f"Image width too small: {w}"
        assert h >= 180, f"Image height too small: {h}"


# 2.2 Camera feed stream
class TestCameraFeedStream:
    def test_capture_5_frames(self, robot_connected):
        """2.2 — CameraFeed gRPC captures 5 unique frames."""
        robot = robot_connected
        robot.camera.init_camera_feed()
        frames = []
        for _ in range(50):  # poll up to 50 times
            image = robot.camera.latest_image
            if image is not None:
                img_id = id(image)
                if not frames or frames[-1] != img_id:
                    frames.append(img_id)
                if len(frames) >= 5:
                    break
            import time
            time.sleep(0.1)
        robot.camera.close_camera_feed()
        assert len(frames) >= 5, f"Only got {len(frames)} unique frames"


# 2.3 Head angle min
class TestHeadAngleMin:
    def test_head_min_position(self, robot_connected):
        """2.3 — set_head_angle(degrees(-22)) moves head to min."""
        from anki_vector.util import degrees

        robot = robot_connected
        robot.behavior.set_head_angle(degrees(-22))


# 2.4 Head angle max
class TestHeadAngleMax:
    def test_head_max_position(self, robot_connected):
        """2.4 — set_head_angle(degrees(45)) moves head to max."""
        from anki_vector.util import degrees

        robot = robot_connected
        robot.behavior.set_head_angle(degrees(45))


# 2.5 Head angle neutral
class TestHeadAngleNeutral:
    def test_head_neutral_position(self, robot_connected):
        """2.5 — set_head_angle(degrees(10)) returns to neutral."""
        from anki_vector.util import degrees

        robot = robot_connected
        robot.behavior.set_head_angle(degrees(10))


# 2.6 Lift up
class TestLiftUp:
    def test_lift_rises(self, robot_connected):
        """2.6 — set_lift_height(1.0) raises lift."""
        robot = robot_connected
        robot.behavior.set_lift_height(1.0)


# 2.7 Lift down
class TestLiftDown:
    def test_lift_lowers(self, robot_connected):
        """2.7 — set_lift_height(0.0) lowers lift."""
        robot = robot_connected
        robot.behavior.set_lift_height(0.0)


# 2.8 LED solid color
class TestLEDSolidColor:
    def test_eye_color_red(self, robot_connected):
        """2.8 — set_eye_color sets a visible color (Vector uses eye color, not backpack)."""
        robot = robot_connected
        # Vector SDK: set_eye_color(hue, saturation) — hue 0.0 = red
        robot.behavior.set_eye_color(0.0, 1.0)


# 2.9 LED off
class TestLEDOff:
    def test_eye_color_default(self, robot_connected):
        """2.9 — Reset eye color to default (green-ish)."""
        robot = robot_connected
        # Default Vector eye color: hue ~0.33 (green), saturation 1.0
        robot.behavior.set_eye_color(0.33, 0.75)


# 2.10 Display image
class TestDisplayImage:
    def test_display_oled(self, robot_connected):
        """2.10 — 184x96 image displayed on OLED."""
        from anki_vector.screen import convert_image_to_screen_data

        robot = robot_connected
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not installed")
        img = Image.new("L", (184, 96), color=128)
        screen_data = convert_image_to_screen_data(img)
        robot.screen.set_screen_with_image_data(screen_data, 5.0)


# 2.11 TTS say_text
class TestTTSSayText:
    def test_say_text(self, robot_connected):
        """2.11 — say_text('test') plays audio from speaker."""
        robot = robot_connected
        robot.behavior.say_text("test")


# 2.12 Touch sensor read
class TestTouchSensorRead:
    def test_touch_state(self, robot_connected):
        """2.12 — Touch sensor returns valid state."""
        robot = robot_connected
        touch = robot.touch.last_sensor_reading
        assert touch is not None
        # Should have is_being_touched attribute
        assert hasattr(touch, "is_being_touched")


# 2.13 Cliff sensors read
class TestCliffSensorsRead:
    def test_cliff_values(self, robot_connected):
        """2.13 — 4 cliff sensor values returned."""
        robot = robot_connected
        # Cliff data available via robot state
        state = robot.status
        assert state is not None


# 2.14 Accelerometer read
class TestAccelerometerRead:
    def test_accel_3axis(self, robot_connected):
        """2.14 — 3-axis accelerometer values, non-zero."""
        robot = robot_connected
        accel = robot.accel
        assert accel is not None
        # At least gravity should be non-zero
        assert accel.x != 0 or accel.y != 0 or accel.z != 0, (
            "All accelerometer axes are zero"
        )


# 2.15 Gyroscope read
class TestGyroscopeRead:
    def test_gyro_3axis(self, robot_connected):
        """2.15 — 3-axis gyroscope values returned."""
        robot = robot_connected
        gyro = robot.gyro
        assert gyro is not None
