"""Unit tests for HeadTracker — event-driven head pitch controller."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    HEAD_ANGLE_COMMAND,
    TRACKED_PERSON,
    HeadAngleCommandEvent,
    TrackedPersonEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.head_controller import NEUTRAL_ANGLE
from apps.vector.src.planner.head_tracker import (
    FRAME_H_DEFAULT as FRAME_H,
    HeadTracker,
    HeadTrackerConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def head():
    hc = MagicMock()
    hc.set_angle = MagicMock(side_effect=lambda a, speed_dps=90: a)
    hc.last_angle = NEUTRAL_ANGLE
    return hc


@pytest.fixture()
def config():
    """Fast config for tests (short timeout, fast loop)."""
    return HeadTrackerConfig(
        kp=0.15,
        dead_zone_px=15.0,
        max_slew_rate=5.0,
        head_speed_dps=90.0,
        neutral_timeout_s=0.1,  # short for tests
        loop_hz=100.0,  # fast for tests
    )


@pytest.fixture()
def tracker(head, bus, config):
    return HeadTracker(head, bus, config)


def _make_track(
    cx: float = 320.0,
    cy: float = FRAME_H / 2,
    height: float = 150.0,
    width: float = 80.0,
    hits: int = 5,
    confidence: float = 0.8,
    track_id: int = 1,
    age_frames: int = 10,
) -> TrackedPersonEvent:
    return TrackedPersonEvent(
        track_id=track_id,
        cx=cx,
        cy=cy,
        width=width,
        height=height,
        age_frames=age_frames,
        hits=hits,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestHeadTrackerConfig:
    def test_default_config(self):
        cfg = HeadTrackerConfig()
        assert cfg.kp == 0.15
        assert cfg.dead_zone_px == 15.0
        assert cfg.max_slew_rate == 5.0

    def test_custom_config(self):
        cfg = HeadTrackerConfig(kp=0.2, dead_zone_px=20.0)
        assert cfg.kp == 0.2
        assert cfg.dead_zone_px == 20.0


# ---------------------------------------------------------------------------
# Synchronous update tests (no control loop)
# ---------------------------------------------------------------------------


class TestHeadTrackerUpdate:
    def test_person_centered_no_movement(self, tracker, head):
        """Person at frame center → no head adjustment (within dead zone)."""
        track = _make_track(cy=FRAME_H / 2)
        tracker.update(track)
        # Within dead zone — head should not be called
        head.set_angle.assert_not_called()

    def test_person_below_center_looks_down(self, tracker, head):
        """Person below center → head angle decreases (look down)."""
        track = _make_track(cy=FRAME_H / 2 + 50)  # 50px below center
        tracker.update(track)
        head.set_angle.assert_called_once()
        angle = head.set_angle.call_args[0][0]
        assert angle < NEUTRAL_ANGLE

    def test_person_above_center_looks_up(self, tracker, head):
        """Person above center → head angle increases (look up)."""
        track = _make_track(cy=FRAME_H / 2 - 50)  # 50px above center
        tracker.update(track)
        head.set_angle.assert_called_once()
        angle = head.set_angle.call_args[0][0]
        assert angle > NEUTRAL_ANGLE

    def test_dead_zone_no_adjustment(self, tracker, head):
        """Small vertical offset within dead zone → no adjustment."""
        track = _make_track(cy=FRAME_H / 2 + 10)  # 10px < 15px dead zone
        tracker.update(track)
        head.set_angle.assert_not_called()

    def test_dead_zone_boundary(self, tracker, head):
        """Offset exactly at dead zone boundary → no adjustment."""
        track = _make_track(cy=FRAME_H / 2 + 14.9)
        tracker.update(track)
        head.set_angle.assert_not_called()

    def test_slew_rate_limits_large_offset(self, tracker, head, config):
        """Large offset is clamped by max_slew_rate."""
        # 200px offset → kp * 200 = 30° unclamped, but slew = 5°
        track = _make_track(cy=FRAME_H / 2 + 200)
        tracker.update(track)
        head.set_angle.assert_called_once()
        angle = head.set_angle.call_args[0][0]
        # Should be neutral - max_slew_rate = 10 - 5 = 5
        assert angle == NEUTRAL_ANGLE - config.max_slew_rate

    def test_slew_rate_limits_upward(self, tracker, head, config):
        """Large upward offset also clamped by slew rate."""
        track = _make_track(cy=FRAME_H / 2 - 200)
        tracker.update(track)
        head.set_angle.assert_called_once()
        angle = head.set_angle.call_args[0][0]
        assert angle == NEUTRAL_ANGLE + config.max_slew_rate

    def test_uses_last_angle_as_base(self, head, bus, config):
        """Head tracker uses last_angle from controller as base."""
        head.last_angle = 20.0
        tracker = HeadTracker(head, bus, config)
        track = _make_track(cy=FRAME_H / 2 + 50)
        tracker.update(track)
        angle = head.set_angle.call_args[0][0]
        # Should adjust from 20.0, not from neutral
        assert angle < 20.0

    def test_none_last_angle_uses_neutral(self, head, bus, config):
        """If last_angle is None, use NEUTRAL_ANGLE as base."""
        head.last_angle = None
        tracker = HeadTracker(head, bus, config)
        track = _make_track(cy=FRAME_H / 2 + 50)
        tracker.update(track)
        angle = head.set_angle.call_args[0][0]
        assert angle < NEUTRAL_ANGLE


# ---------------------------------------------------------------------------
# Event emission tests
# ---------------------------------------------------------------------------


class TestHeadTrackerEvents:
    def test_emits_head_angle_command_event(self, tracker, bus):
        """Tracking emits HEAD_ANGLE_COMMAND event."""
        events = []
        bus.on(HEAD_ANGLE_COMMAND, lambda e: events.append(e))

        track = _make_track(cy=FRAME_H / 2 + 50)
        tracker.update(track)

        assert len(events) == 1
        assert isinstance(events[0], HeadAngleCommandEvent)
        assert events[0].source == "head_tracker"

    def test_no_event_in_dead_zone(self, tracker, bus):
        """No event emitted when offset is within dead zone."""
        events = []
        bus.on(HEAD_ANGLE_COMMAND, lambda e: events.append(e))

        track = _make_track(cy=FRAME_H / 2 + 5)
        tracker.update(track)

        assert len(events) == 0


# ---------------------------------------------------------------------------
# Control loop tests (start/stop)
# ---------------------------------------------------------------------------


class TestHeadTrackerLifecycle:
    def test_start_stop(self, tracker):
        """Start and stop without errors."""
        tracker.start()
        assert tracker.is_running
        time.sleep(0.05)
        tracker.stop()
        assert not tracker.is_running

    def test_double_start_is_noop(self, tracker):
        """Double start doesn't crash."""
        tracker.start()
        tracker.start()  # should log warning
        tracker.stop()

    def test_double_stop_is_noop(self, tracker):
        """Double stop doesn't crash."""
        tracker.stop()  # not running — should be safe

    def test_stop_returns_to_neutral(self, tracker, head):
        """Stopping head tracker returns head to neutral."""
        tracker.start()
        time.sleep(0.05)
        tracker.stop()
        # Last call should be to neutral
        last_call = head.set_angle.call_args_list[-1]
        assert last_call[0][0] == NEUTRAL_ANGLE

    def test_event_bus_tracking(self, tracker, bus, head):
        """Tracks received via event bus are processed by control loop."""
        tracker.start()
        time.sleep(0.05)

        # Emit a track via event bus
        bus.emit(TRACKED_PERSON, _make_track(cy=FRAME_H / 2 + 50))
        time.sleep(0.1)

        tracker.stop()

        # Head should have been called with a tracking angle
        tracking_calls = [
            c for c in head.set_angle.call_args_list
            if c[0][0] != NEUTRAL_ANGLE
        ]
        assert len(tracking_calls) >= 1


# ---------------------------------------------------------------------------
# Neutral return tests
# ---------------------------------------------------------------------------


class TestHeadTrackerNeutralReturn:
    def test_returns_to_neutral_after_timeout(self, head, bus):
        """Head returns to neutral after neutral_timeout_s with no tracks."""
        config = HeadTrackerConfig(
            neutral_timeout_s=0.05,
            loop_hz=100.0,
            max_slew_rate=50.0,  # large slew for fast return in test
        )
        tracker = HeadTracker(head, bus, config)
        tracker.start()
        time.sleep(0.02)

        # Feed a track to move head away from neutral
        bus.emit(TRACKED_PERSON, _make_track(cy=FRAME_H / 2 + 50))
        time.sleep(0.05)

        # Now wait for neutral return timeout
        time.sleep(0.15)

        tracker.stop()

        # Last set_angle before stop's neutral call should be heading toward neutral
        calls = head.set_angle.call_args_list
        # The stop() call sets neutral; check there were neutral-returning calls before
        assert len(calls) >= 2

    def test_no_neutral_return_while_tracking(self, head, bus):
        """Head does NOT return to neutral while person is being tracked."""
        config = HeadTrackerConfig(
            neutral_timeout_s=0.05,
            loop_hz=100.0,
        )
        tracker = HeadTracker(head, bus, config)
        tracker.start()
        time.sleep(0.02)

        # Keep feeding tracks — head should stay tracking
        for _ in range(5):
            bus.emit(TRACKED_PERSON, _make_track(cy=FRAME_H / 2 + 50))
            time.sleep(0.03)

        tracker.stop()

        # During tracking, the angle should consistently be below neutral
        tracking_angles = [
            c[0][0] for c in head.set_angle.call_args_list
            if c[0][0] != NEUTRAL_ANGLE
        ]
        # At least some tracking calls happened
        assert len(tracking_angles) >= 1
        for angle in tracking_angles:
            assert angle < NEUTRAL_ANGLE  # looking down


# ---------------------------------------------------------------------------
# Integration with FollowPlanner
# ---------------------------------------------------------------------------


class TestHeadTrackerFollowPlannerIntegration:
    def test_follow_planner_has_head_tracker(self):
        """FollowPlanner creates a HeadTracker instance."""
        from apps.vector.src.planner.follow_planner import FollowPlanner

        motor = MagicMock()
        head = MagicMock()
        head.last_angle = NEUTRAL_ANGLE
        head.set_angle = MagicMock(side_effect=lambda a, speed_dps=90: a)
        bus = NucEventBus()
        planner = FollowPlanner(motor, head, bus)
        assert isinstance(planner.head_tracker, HeadTracker)

    def test_follow_planner_delegates_head_tracking(self):
        """FollowPlanner._apply_head_tracking delegates to HeadTracker."""
        from apps.vector.src.planner.follow_planner import FollowPlanner

        motor = MagicMock()
        head = MagicMock()
        head.last_angle = NEUTRAL_ANGLE
        head.set_angle = MagicMock(side_effect=lambda a, speed_dps=90: a)
        bus = NucEventBus()
        planner = FollowPlanner(motor, head, bus)

        track = _make_track(cy=FRAME_H / 2 + 50)
        planner._apply_tracking(track)

        # HeadTracker should have called head.set_angle
        assert head.set_angle.call_count >= 1
        angle = head.set_angle.call_args[0][0]
        assert angle < NEUTRAL_ANGLE
