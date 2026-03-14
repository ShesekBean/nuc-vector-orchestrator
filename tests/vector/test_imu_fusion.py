"""Unit tests for ImuFusion complementary filter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import ImuUpdateEvent, MotorCommandEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.imu_fusion import ImuFusion


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def fusion(bus):
    f = ImuFusion(bus, alpha=0.85)
    f.start()
    yield f
    f.stop()


class TestImuFusion:
    def test_initial_pose_is_zero(self, fusion):
        pose = fusion.get_fused_pose()
        assert pose.x == 0.0
        assert pose.y == 0.0
        assert pose.theta == 0.0

    def test_gyro_integrates_heading(self, fusion, bus):
        """Gyro Z rotation should update heading."""
        from apps.vector.src.events.event_types import IMU_UPDATE

        # First event sets baseline time
        bus.emit(IMU_UPDATE, ImuUpdateEvent(
            accel_x=0, accel_y=0, accel_z=1.0,
            gyro_x=0, gyro_y=0, gyro_z=0,
        ))

        import time
        time.sleep(0.05)

        # Rotate at 90 deg/s for one update (dt ≈ 0.05s)
        bus.emit(IMU_UPDATE, ImuUpdateEvent(
            accel_x=0, accel_y=0, accel_z=1.0,
            gyro_x=0, gyro_y=0, gyro_z=90.0,  # 90 deg/s CW
        ))

        pose = fusion.get_fused_pose()
        # Theta should have changed (negative because SDK gyro CW = negative CCW)
        assert pose.theta != 0.0

    def test_gyro_noise_filtered(self, fusion, bus):
        """Small gyro readings below noise threshold are ignored."""
        from apps.vector.src.events.event_types import IMU_UPDATE

        bus.emit(IMU_UPDATE, ImuUpdateEvent(
            accel_x=0, accel_y=0, accel_z=1.0,
            gyro_x=0, gyro_y=0, gyro_z=0,
        ))

        import time
        time.sleep(0.02)

        # Very small rotation — below threshold (1.5 dps)
        bus.emit(IMU_UPDATE, ImuUpdateEvent(
            accel_x=0, accel_y=0, accel_z=1.0,
            gyro_x=0, gyro_y=0, gyro_z=0.5,
        ))

        pose = fusion.get_fused_pose()
        assert pose.theta == 0.0

    def test_motor_updates_position(self, fusion, bus):
        """Motor commands should update x/y position."""
        from apps.vector.src.events.event_types import MOTOR_COMMAND

        # Drive forward at 100 mm/s for 100ms
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(
            left_speed_mmps=100, right_speed_mmps=100, duration_ms=100,
        ))

        pose = fusion.get_fused_pose()
        # Should have moved ~10mm forward (100 mm/s * 0.1s)
        assert pose.x > 0

    def test_visual_correction_blends(self, fusion):
        """Visual correction should shift heading toward visual estimate."""
        # Set heading manually
        fusion._pose.theta = 0.5  # 0.5 radians

        # Visual says heading is 0.3 radians
        fusion.apply_visual_correction(0.3)

        pose = fusion.get_fused_pose()
        # Should be between 0.3 and 0.5 (blended with alpha=0.85)
        assert 0.3 < pose.theta < 0.5

    def test_reset_pose(self, fusion):
        fusion._pose.x = 100
        fusion._pose.y = 200
        fusion.reset_pose(x=50, y=60, theta=1.0)

        pose = fusion.get_fused_pose()
        assert pose.x == 50
        assert pose.y == 60
        assert pose.theta == 1.0

    def test_update_counts(self, fusion, bus):
        from apps.vector.src.events.event_types import IMU_UPDATE, MOTOR_COMMAND

        bus.emit(IMU_UPDATE, ImuUpdateEvent(0, 0, 1, 0, 0, 0))
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(100, 100, 100))

        assert fusion.imu_update_count >= 1
        assert fusion.motor_update_count >= 1
