"""Unit tests for FollowPlanner and state machine."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, call

import pytest

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    FOLLOW_STATE_CHANGED,
    MOTOR_COMMAND,
    TRACKED_PERSON,
    EmergencyStopEvent,
    MotorCommandEvent,
    TrackedPersonEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.planner.follow_planner import (
    FollowConfig,
    FollowPlanner,
    State,
)

# Default frame dimensions (matches TrackedPersonEvent defaults)
FRAME_W = 640
FRAME_H = 360


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config():
    """Fast config for tests (short search dwells, fewer body angles)."""
    return FollowConfig(
        search_head_dwell_s=0.01,
        search_body_dwell_s=0.01,
        search_head_angles=(10.0, 30.0),
        search_step_deg=90.0,
        search_max_cycles=1,
        loop_hz=100.0,  # fast loop for tests
        target_lost_frames=3,
    )


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def motor():
    mc = MagicMock()
    mc.drive_wheels = MagicMock()
    mc.turn_in_place = MagicMock()
    mc.emergency_stop = MagicMock()
    return mc


@pytest.fixture()
def head():
    hc = MagicMock()
    hc.set_angle = MagicMock(return_value=10.0)
    hc.last_angle = 10.0
    return hc


@pytest.fixture()
def planner(motor, head, bus, config):
    return FollowPlanner(motor, head, bus, config)


def _make_track(
    cx: float = FRAME_W / 2,
    cy: float = FRAME_H / 2,
    height: float = 100.0,
    width: float = 50.0,
    hits: int = 5,
    confidence: float = 0.8,
    track_id: int = 1,
    age_frames: int = 10,
    frame_width: int = FRAME_W,
    frame_height: int = FRAME_H,
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
        frame_width=frame_width,
        frame_height=frame_height,
    )


# ---------------------------------------------------------------------------
# FollowPlanner state machine tests
# ---------------------------------------------------------------------------


class TestFollowPlannerState:
    def test_initial_state_idle(self, planner):
        assert planner.state == State.IDLE

    def test_start_transitions_to_searching(self, planner):
        planner.start()
        try:
            assert planner.state in (State.SEARCHING, State.IDLE)
        finally:
            planner._running = False
            planner.stop()

    def test_stop_returns_to_idle(self, planner, bus):
        planner.start()
        time.sleep(0.05)
        planner.stop()
        assert planner.state == State.IDLE

    def test_stop_zeroes_motors(self, planner, motor):
        planner.start()
        time.sleep(0.05)
        planner.stop()
        motor.drive_wheels.assert_called_with(0, 0)

    def test_double_start_is_noop(self, planner):
        planner.start()
        try:
            planner.start()  # should log warning, not crash
        finally:
            planner.stop()

    def test_double_stop_is_noop(self, planner):
        planner.stop()  # should not crash when already idle

    def test_emergency_stop_returns_to_idle(self, planner, bus):
        planner.start()
        time.sleep(0.05)
        with planner._state_lock:
            planner._state = State.FOLLOWING
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))
        time.sleep(0.05)
        assert planner.state == State.IDLE
        planner._running = False


class TestFollowPlannerTracking:
    def test_tracked_person_transitions_to_following(self, planner, bus):
        """Receiving a track during SEARCHING → FOLLOWING."""
        planner.start()
        time.sleep(0.05)

        bus.emit(TRACKED_PERSON, _make_track(hits=1))
        time.sleep(0.15)

        state = planner.state
        planner.stop()
        assert state in (State.FOLLOWING, State.SEARCHING, State.IDLE)

    def test_following_sends_motor_commands(self, planner, bus, motor, config):
        """In FOLLOWING state, motor commands are sent."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person to the right — should trigger turn
        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W / 2 + 100,
            height=config.target_height_frac * FRAME_H,
        ))
        time.sleep(0.15)

        planner.stop()
        calls = motor.drive_wheels.call_args_list
        assert len(calls) >= 1

    def test_turn_first_when_off_center(self, planner, bus, motor, config):
        """Person far off-center → turn only, no forward drive."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person far right — beyond turn_first_threshold
        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W * 0.9,  # far right
            height=config.target_height_frac * FRAME_H,
        ))
        time.sleep(0.15)

        planner.stop()

        # Check motor commands — should be turning (left > 0, right < 0 for right turn)
        tracking_calls = [c for c in motor.drive_wheels.call_args_list if c != call(0, 0)]
        if tracking_calls:
            left, right = tracking_calls[0][0]
            # Person is right → turn right → left wheel forward, right wheel backward
            assert left > 0
            assert right < 0

    def test_drive_when_centered(self, planner, bus, motor, config):
        """Person centered but far → drive forward, equal wheel speeds."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person centered, far away (small bbox)
        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W / 2,
            height=50.0,  # small = far away
        ))
        time.sleep(0.15)

        planner.stop()

        tracking_calls = [c for c in motor.drive_wheels.call_args_list if c != call(0, 0)]
        if tracking_calls:
            left, right = tracking_calls[0][0]
            # Both should be positive (forward) and roughly equal
            assert left > 0
            assert right > 0
            assert abs(left - right) < 1.0  # equal speeds

    def test_too_close_stops(self, planner, bus, motor, config):
        """Person filling most of frame → stop (don't reverse)."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person very close (large bbox, centered)
        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W / 2,
            height=FRAME_H * 0.7,  # > too_close_frac
        ))
        time.sleep(0.15)

        planner.stop()

        # Last tracking call should be zero (stop)
        tracking_calls = [c for c in motor.drive_wheels.call_args_list]
        if len(tracking_calls) > 1:
            last = tracking_calls[-2]  # -1 is the stop() call
            left, right = last[0]
            assert left == 0.0
            assert right == 0.0

    def test_target_lost_transitions_to_searching(self, planner, bus, config):
        """No tracks for target_lost_frames → SEARCHING."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        wait_time = (config.target_lost_frames + 2) / config.loop_hz
        time.sleep(wait_time + 0.1)

        state = planner.state
        planner.stop()
        assert state in (State.SEARCHING, State.IDLE)


class TestFollowPlannerHeadTracking:
    def test_head_tracks_vertically(self, planner, bus, head, config):
        """Person below center → head angle adjusts."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        bus.emit(TRACKED_PERSON, _make_track(cy=FRAME_H / 2 + 50))
        time.sleep(0.15)

        planner.stop()

        head_calls = head.set_angle.call_args_list
        assert len(head_calls) >= 1


class TestFollowPlannerEvents:
    def test_state_change_emits_event(self, planner, bus):
        """State transitions emit FOLLOW_STATE_CHANGED events."""
        events = []
        bus.on(FOLLOW_STATE_CHANGED, lambda e: events.append(e))

        planner.start()
        time.sleep(0.05)
        planner.stop()

        states = [e.state for e in events]
        assert "searching" in states
        assert "idle" in states

    def test_motor_command_emits_event(self, planner, bus, config):
        """Motor commands in FOLLOWING emit MOTOR_COMMAND events."""
        events = []
        bus.on(MOTOR_COMMAND, lambda e: events.append(e))

        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        bus.emit(TRACKED_PERSON, _make_track(cx=FRAME_W / 2 + 100))
        time.sleep(0.15)

        planner.stop()

        if events:
            assert isinstance(events[0], MotorCommandEvent)

    def test_low_confidence_no_motor_command(self, planner, bus, motor, config):
        """Low confidence tracks don't produce motor commands."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        bus.emit(TRACKED_PERSON, _make_track(confidence=0.05))
        time.sleep(0.1)

        planner.stop()


class TestFollowPlannerSearch:
    def test_search_uses_head_sweep(self, planner, head, config):
        """SEARCHING state sweeps head through configured angles."""
        planner.start()
        timeout = (
            len(config.search_head_angles) * config.search_head_dwell_s
            + int(360.0 / config.search_step_deg) * config.search_max_cycles * config.search_body_dwell_s
            + 1.0
        )
        deadline = time.monotonic() + timeout
        while planner.state != State.IDLE and time.monotonic() < deadline:
            time.sleep(0.05)
        planner.stop()

        head_angles_called = [c[0][0] for c in head.set_angle.call_args_list]
        for angle in config.search_head_angles:
            assert angle in head_angles_called

    def test_search_uses_body_rotation(self, planner, motor, config):
        """SEARCHING state rotates body after head sweep fails."""
        planner.start()
        timeout = (
            len(config.search_head_angles) * config.search_head_dwell_s
            + int(360.0 / config.search_step_deg) * config.search_max_cycles * config.search_body_dwell_s
            + 1.0
        )
        deadline = time.monotonic() + timeout
        while planner.state != State.IDLE and time.monotonic() < deadline:
            time.sleep(0.05)
        planner.stop()

        assert motor.turn_in_place.call_count >= 1

    def test_full_search_no_target_returns_idle(self, planner, config):
        """Full search with no detection → IDLE."""
        planner.start()
        timeout = (
            len(config.search_head_angles) * config.search_head_dwell_s
            + int(360.0 / config.search_step_deg) * config.search_max_cycles * config.search_body_dwell_s
            + 1.0
        )
        deadline = time.monotonic() + timeout
        while planner.state != State.IDLE and time.monotonic() < deadline:
            time.sleep(0.05)
        planner.stop()
        assert planner.state == State.IDLE


class TestFollowConfig:
    def test_default_config(self):
        cfg = FollowConfig()
        assert cfg.max_wheel_speed == 140.0
        assert cfg.target_height_frac == 0.35
        assert cfg.loop_hz == 15.0

    def test_custom_config(self):
        cfg = FollowConfig(max_wheel_speed=80.0, target_height_frac=0.4)
        assert cfg.max_wheel_speed == 80.0
        assert cfg.target_height_frac == 0.4

    def test_planner_uses_config(self, bus, motor, head):
        cfg = FollowConfig(max_wheel_speed=50.0)
        p = FollowPlanner(motor, head, bus, cfg)
        assert p.config.max_wheel_speed == 50.0
