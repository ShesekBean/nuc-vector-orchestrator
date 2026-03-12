"""Unit tests for ObstacleDetector — camera-based obstacle avoidance."""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    MOTOR_COMMAND,
    OBSTACLE_DETECTED,
    CliffTriggeredEvent,
    MotorCommandEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.obstacle_detector import (
    ESCAPE_BACKUP_MM,
    ESCAPE_ROTATE_DEG,
    FRAME_AREA,
    FRAME_H,
    FRAME_W,
    ObstacleConfig,
    ObstacleDetector,
)


# ---------------------------------------------------------------------------
# Test Detection dataclass (mimics kalman_tracker.Detection with class_id)
# ---------------------------------------------------------------------------


@dataclass
class FakeDetection:
    """Detection with optional class_id for obstacle filtering."""

    cx: float
    cy: float
    width: float
    height: float
    confidence: float
    class_id: int | None = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def motor():
    mc = MagicMock()
    mc.drive_wheels = MagicMock()
    mc.drive_straight = MagicMock()
    mc.turn_in_place = MagicMock()
    mc.emergency_stop = MagicMock()
    mc.clear_stop = MagicMock()
    return mc


@pytest.fixture()
def config():
    """Config with fast timings for tests."""
    return ObstacleConfig(
        danger_threshold=0.25,
        caution_threshold=0.10,
        min_confidence=0.4,
        confirm_frames=2,  # faster for tests
        stuck_timeout_s=0.1,  # fast for tests
        max_escape_attempts=3,
    )


@pytest.fixture()
def detector(motor, bus, config):
    d = ObstacleDetector(motor, bus, config)
    d.start()
    yield d
    d.stop()


def _obstacle_in_center(
    area_ratio: float, confidence: float = 0.8, class_id: int = 56
) -> list[FakeDetection]:
    """Create a single obstacle detection centered in the frame."""
    # Compute bbox dimensions that produce the desired area ratio
    area = area_ratio * FRAME_AREA
    side = area**0.5
    return [
        FakeDetection(
            cx=FRAME_W / 2,
            cy=FRAME_H / 2,
            width=side,
            height=side,
            confidence=confidence,
            class_id=class_id,
        )
    ]


def _person_in_center(area_ratio: float = 0.15) -> list[FakeDetection]:
    """Create a person detection (class_id=0) — should be excluded."""
    area = area_ratio * FRAME_AREA
    side = area**0.5
    return [
        FakeDetection(
            cx=FRAME_W / 2,
            cy=FRAME_H / 2,
            width=side,
            height=side,
            confidence=0.9,
            class_id=0,  # person
        )
    ]


# ---------------------------------------------------------------------------
# Zone detection tests
# ---------------------------------------------------------------------------


class TestZoneDetection:
    def test_clear_zone_with_no_detections(self, detector):
        scale = detector.update([])
        assert scale == 1.0
        assert detector.zone == "clear"

    def test_clear_zone_with_small_obstacle(self, detector):
        """Obstacle below caution threshold → clear."""
        for _ in range(3):
            scale = detector.update(_obstacle_in_center(0.05))
        assert scale == 1.0
        assert detector.zone == "clear"

    def test_caution_zone(self, detector, config):
        """Obstacle in caution range → proportional slowdown."""
        area = (config.caution_threshold + config.danger_threshold) / 2
        for _ in range(config.confirm_frames):
            scale = detector.update(_obstacle_in_center(area))
        assert detector.zone == "caution"
        assert 0.0 < scale < 1.0

    def test_danger_zone(self, detector, config):
        """Obstacle above danger threshold → full stop."""
        for _ in range(config.confirm_frames):
            scale = detector.update(_obstacle_in_center(0.30))
        assert detector.zone == "danger"
        assert scale == 0.0

    def test_caution_scale_interpolation(self, detector, config):
        """Scale should vary linearly within caution zone."""
        # At caution boundary → max scale
        for _ in range(config.confirm_frames):
            detector.update(_obstacle_in_center(config.caution_threshold))
        scale_at_caution = detector.speed_scale
        assert scale_at_caution == pytest.approx(config.caution_max_scale, abs=0.01)

        # At danger boundary → min scale
        detector.stop()
        detector.start()
        for _ in range(config.confirm_frames):
            detector.update(_obstacle_in_center(config.danger_threshold - 0.001))
        scale_at_danger = detector.speed_scale
        assert scale_at_danger < scale_at_caution

    def test_zone_transitions_back_to_clear(self, detector, config):
        """Obstacle disappearing → zone returns to clear."""
        for _ in range(config.confirm_frames):
            detector.update(_obstacle_in_center(0.30))
        assert detector.zone == "danger"

        # Clear frames
        for _ in range(config.confirm_frames):
            detector.update([])
        assert detector.zone == "clear"
        assert detector.speed_scale == 1.0


# ---------------------------------------------------------------------------
# Filtering tests
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_person_excluded(self, detector, config):
        """Person detections (class_id=0) should not trigger obstacle zone."""
        for _ in range(config.confirm_frames + 1):
            scale = detector.update(_person_in_center(0.30))
        assert scale == 1.0
        assert detector.zone == "clear"

    def test_low_confidence_excluded(self, detector, config):
        """Low confidence detections should be ignored."""
        for _ in range(config.confirm_frames + 1):
            scale = detector.update(_obstacle_in_center(0.30, confidence=0.1))
        assert scale == 1.0

    def test_outside_forward_cone_excluded(self, detector, config):
        """Obstacles outside the forward cone should be ignored."""
        area = 0.30 * FRAME_AREA
        side = area**0.5
        # Place obstacle at far left (outside center 60%)
        det = FakeDetection(
            cx=10.0, cy=FRAME_H / 2, width=side, height=side,
            confidence=0.9, class_id=56,
        )
        for _ in range(config.confirm_frames + 1):
            scale = detector.update([det])
        assert scale == 1.0

    def test_confirm_frames_debounce(self, detector, config):
        """Zone should only change after confirm_frames consecutive frames."""
        # First frame — not confirmed yet
        detector.update(_obstacle_in_center(0.30))
        assert detector.zone == "clear"

        # Second frame — confirmed (confirm_frames=2)
        detector.update(_obstacle_in_center(0.30))
        assert detector.zone == "danger"

    def test_detection_without_class_id(self, detector, config):
        """Detection without class_id attribute → treated as obstacle."""
        area = 0.30 * FRAME_AREA
        side = area**0.5
        det = FakeDetection(
            cx=FRAME_W / 2, cy=FRAME_H / 2, width=side, height=side,
            confidence=0.9, class_id=None,
        )
        for _ in range(config.confirm_frames):
            detector.update([det])
        assert detector.zone == "danger"


# ---------------------------------------------------------------------------
# Event emission tests
# ---------------------------------------------------------------------------


class TestEvents:
    def test_emits_obstacle_event_on_zone_change(self, detector, bus, config):
        events = []
        bus.on(OBSTACLE_DETECTED, lambda e: events.append(e))

        for _ in range(config.confirm_frames):
            detector.update(_obstacle_in_center(0.30))

        assert len(events) == 1
        assert events[0].zone == "danger"
        assert events[0].speed_scale == 0.0

    def test_no_event_on_same_zone(self, detector, bus, config):
        """No event emitted if zone doesn't change between updates."""
        events = []
        bus.on(OBSTACLE_DETECTED, lambda e: events.append(e))

        for _ in range(config.confirm_frames + 3):
            detector.update(_obstacle_in_center(0.30))

        # Only one event (the initial transition from clear → danger)
        assert len(events) == 1

    def test_cliff_event_sets_danger(self, detector, bus):
        """Cliff event should immediately set danger zone."""
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=0x01))
        assert detector.zone == "danger"
        assert detector.speed_scale == 0.0


# ---------------------------------------------------------------------------
# Stuck detection + escape maneuver tests
# ---------------------------------------------------------------------------


class TestStuckDetection:
    def test_not_stuck_when_idle(self, detector):
        assert not detector.is_stuck

    def test_becomes_stuck_after_timeout(self, detector, bus, config):
        """Motor commands with no track movement → stuck."""
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
        time.sleep(config.stuck_timeout_s + 0.05)
        assert detector.is_stuck

    def test_reset_stuck_clears(self, detector, bus, config):
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
        time.sleep(config.stuck_timeout_s + 0.05)
        detector.reset_stuck()
        assert not detector.is_stuck

    def test_zero_speed_not_stuck(self, detector, bus, config):
        """Zero speed motor commands should not trigger stuck."""
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=0, right_speed_mmps=0))
        time.sleep(config.stuck_timeout_s + 0.05)
        assert not detector.is_stuck

    def test_escape_maneuver_executes(self, detector, motor, bus, config):
        """check_stuck triggers escape: stop → backup → rotate."""
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
        time.sleep(config.stuck_timeout_s + 0.05)

        escaped = detector.check_stuck()
        assert escaped
        assert detector.escape_count == 1

        motor.emergency_stop.assert_called()
        motor.clear_stop.assert_called()
        motor.drive_straight.assert_called_once_with(-ESCAPE_BACKUP_MM, 60.0)
        motor.turn_in_place.assert_called_once_with(ESCAPE_ROTATE_DEG)

    def test_max_escape_attempts(self, detector, motor, bus, config):
        """Escape should stop after max_escape_attempts."""
        for i in range(config.max_escape_attempts):
            bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
            time.sleep(config.stuck_timeout_s + 0.05)
            escaped = detector.check_stuck()
            assert escaped

        # Next attempt should be blocked
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
        time.sleep(config.stuck_timeout_s + 0.05)
        escaped = detector.check_stuck()
        assert not escaped
        assert detector.escape_count == config.max_escape_attempts


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_resets_state(self, motor, bus, config):
        d = ObstacleDetector(motor, bus, config)
        d.start()
        for _ in range(config.confirm_frames):
            d.update(_obstacle_in_center(0.30))
        assert d.zone == "danger"

        d.stop()
        assert d.speed_scale == 1.0
        assert d.zone == "clear"

    def test_double_start_is_safe(self, detector):
        detector.start()  # already started in fixture

    def test_double_stop_is_safe(self, motor, bus, config):
        d = ObstacleDetector(motor, bus, config)
        d.stop()
        d.stop()

    def test_no_events_after_stop(self, motor, bus, config):
        d = ObstacleDetector(motor, bus, config)
        d.start()
        d.stop()
        # Motor event should not affect state
        bus.emit(MOTOR_COMMAND, MotorCommandEvent(left_speed_mmps=50, right_speed_mmps=50))
        assert not d.is_stuck


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestObstacleConfig:
    def test_default_config(self):
        cfg = ObstacleConfig()
        assert cfg.danger_threshold == 0.25
        assert cfg.caution_threshold == 0.10
        assert cfg.stuck_timeout_s == 2.0
        assert cfg.max_escape_attempts == 3
        assert cfg.person_class == 0

    def test_custom_config(self):
        cfg = ObstacleConfig(danger_threshold=0.30, stuck_timeout_s=5.0)
        assert cfg.danger_threshold == 0.30
        assert cfg.stuck_timeout_s == 5.0

    def test_detector_uses_config(self, motor, bus):
        cfg = ObstacleConfig(danger_threshold=0.50)
        d = ObstacleDetector(motor, bus, cfg)
        assert d.config.danger_threshold == 0.50
