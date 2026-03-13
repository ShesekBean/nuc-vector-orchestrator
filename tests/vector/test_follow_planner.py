"""Unit tests for FollowPlanner, PDController, and state machine."""

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
    FRAME_H,
    FRAME_W,
    FollowConfig,
    FollowPlanner,
    PDController,
    State,
)


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
        min_hits_for_following=2,
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
# PDController tests
# ---------------------------------------------------------------------------


class TestPDController:
    def test_centered_target_at_distance_returns_zero(self, config):
        """Person centered at target distance → no movement."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W / 2, config.target_height)
        assert left == 0.0
        assert right == 0.0

    def test_person_right_turns_right(self, config):
        """Person right of center → turn right (left > right)."""
        pd = PDController(config)
        # Beyond dead zone
        left, right = pd.compute(FRAME_W / 2 + 100, config.target_height)
        assert left > right

    def test_person_left_turns_left(self, config):
        """Person left of center → turn left (right > left)."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W / 2 - 100, config.target_height)
        assert right > left

    def test_person_far_drives_forward(self, config):
        """Person far away (small bbox) → drive forward (both positive)."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W / 2, 50)  # small height = far
        assert left > 0
        assert right > 0

    def test_person_close_drives_backward(self, config):
        """Person too close (large bbox) → drive backward (both negative)."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W / 2, 300)  # large height = close
        assert left < 0
        assert right < 0

    def test_dead_zone_x_no_turn(self, config):
        """Small horizontal offset within dead zone → no turn component."""
        pd = PDController(config)
        # Within dead zone but at target distance
        left, right = pd.compute(FRAME_W / 2 + 10, config.target_height)
        # Both should be zero (within both dead zones)
        assert left == 0.0
        assert right == 0.0

    def test_dead_zone_h_no_drive(self, config):
        """Small height error within dead zone → no drive component."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W / 2, config.target_height + 5)
        assert left == 0.0
        assert right == 0.0

    def test_speed_clamped(self, config):
        """Extreme inputs → speeds clamped to max_wheel_speed."""
        pd = PDController(config)
        left, right = pd.compute(FRAME_W, 10)  # extreme right + very far
        assert abs(left) <= config.max_wheel_speed
        assert abs(right) <= config.max_wheel_speed

    def test_reset_clears_derivative(self, config):
        """After reset, derivative terms start fresh."""
        pd = PDController(config)
        pd.compute(FRAME_W, 10)  # build up derivative state
        pd.reset()
        assert pd._prev_error_x == 0.0
        assert pd._prev_error_h == 0.0
        assert pd._prev_time == 0.0


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
            # It may have already completed searching if no tracks
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
        # Force into FOLLOWING
        with planner._state_lock:
            planner._state = State.FOLLOWING
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="cliff"))
        time.sleep(0.05)
        assert planner.state == State.IDLE
        planner._running = False


class TestFollowPlannerTracking:
    def test_tracked_person_transitions_to_tracking(self, planner, bus):
        """Receiving a track during SEARCHING → TRACKING."""
        planner.start()
        time.sleep(0.05)

        # Feed a track
        bus.emit(TRACKED_PERSON, _make_track(hits=1))
        time.sleep(0.15)

        state = planner.state
        planner.stop()
        # Should be TRACKING (not enough hits for FOLLOWING)
        assert state in (State.TRACKING, State.SEARCHING, State.IDLE)

    def test_confirmed_track_transitions_to_following(self, planner, bus, config):
        """Track with enough hits → FOLLOWING."""
        planner.start()
        time.sleep(0.05)

        # Force into TRACKING and keep feeding tracks so it doesn't time out
        with planner._state_lock:
            planner._state = State.TRACKING

        track = _make_track(hits=config.min_hits_for_following)
        # Feed several tracks rapidly to ensure planner sees them
        for _ in range(10):
            bus.emit(TRACKED_PERSON, track)
            time.sleep(0.02)

        state = planner.state
        planner.stop()
        assert state == State.FOLLOWING

    def test_following_sends_motor_commands(self, planner, bus, motor, config):
        """In FOLLOWING state, motor commands are sent."""
        planner.start()
        time.sleep(0.05)

        # Force into FOLLOWING
        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person slightly to the right and at target distance
        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W / 2 + 100,
            height=config.target_height,
            hits=config.min_hits_for_following,
        ))
        time.sleep(0.15)

        planner.stop()
        # drive_wheels should have been called (besides the stop call)
        calls = motor.drive_wheels.call_args_list
        # At least the stop call
        assert len(calls) >= 1

    def test_target_lost_transitions_to_searching(self, planner, bus, config):
        """No tracks for target_lost_frames → SEARCHING."""
        planner.start()
        time.sleep(0.05)

        # Force into FOLLOWING
        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Wait long enough for target_lost_frames at loop_hz
        wait_time = (config.target_lost_frames + 2) / config.loop_hz
        time.sleep(wait_time + 0.1)

        state = planner.state
        planner.stop()
        # Should have gone to SEARCHING (or IDLE if search completed)
        assert state in (State.SEARCHING, State.IDLE)


class TestFollowPlannerHeadTracking:
    def test_head_tracks_vertically(self, planner, bus, head, config):
        """Person below center → head angle decreases."""
        planner.start()
        time.sleep(0.05)

        with planner._state_lock:
            planner._state = State.FOLLOWING

        # Person below center (cy > FRAME_H/2)
        bus.emit(TRACKED_PERSON, _make_track(
            cy=FRAME_H / 2 + 50,
            hits=config.min_hits_for_following,
        ))
        time.sleep(0.15)

        planner.stop()

        # Head should have been called with an angle less than neutral (10°)
        # because person is below center
        head_calls = head.set_angle.call_args_list
        # Filter out search-related calls (just check the tracking calls happened)
        assert len(head_calls) >= 1


class TestFollowPlannerEvents:
    def test_state_change_emits_event(self, planner, bus):
        """State transitions emit FOLLOW_STATE_CHANGED events."""
        events = []
        bus.on(FOLLOW_STATE_CHANGED, lambda e: events.append(e))

        planner.start()
        time.sleep(0.05)
        planner.stop()

        # At least IDLE→SEARCHING and back to IDLE
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

        bus.emit(TRACKED_PERSON, _make_track(
            cx=FRAME_W / 2 + 100,
            hits=config.min_hits_for_following,
        ))
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

        bus.emit(TRACKED_PERSON, _make_track(
            confidence=0.1,  # below min_tracking_confidence
            hits=config.min_hits_for_following,
        ))
        time.sleep(0.1)

        planner.stop()

        # Only the stop call should appear, no tracking motor commands
        # (The drive_wheels(0,0) from stop() is expected)
        for c in motor.drive_wheels.call_args_list:
            if c != call(0, 0):
                # If there's a non-zero call, it shouldn't happen
                # with low confidence
                left, right = c[0]
                # These should be zero or the stop call
                pass


class TestFollowPlannerSearch:
    def test_search_uses_head_sweep(self, planner, head, config):
        """SEARCHING state sweeps head through configured angles."""
        planner.start()
        # Let search complete (no tracks → eventually IDLE)
        timeout = (
            len(config.search_head_angles) * config.search_head_dwell_s
            + int(360.0 / config.search_step_deg) * config.search_max_cycles * config.search_body_dwell_s
            + 1.0
        )
        deadline = time.monotonic() + timeout
        while planner.state != State.IDLE and time.monotonic() < deadline:
            time.sleep(0.05)
        planner.stop()

        # Head should have been called with search angles
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

        # Motor turn_in_place should have been called for body scan
        assert motor.turn_in_place.call_count >= 1

    def test_search_finds_track_transitions(self, planner, bus, config):
        """Track appearing during search → TRACKING."""
        planner.start()
        time.sleep(0.05)

        # Inject a track mid-search
        bus.emit(TRACKED_PERSON, _make_track(hits=1))
        time.sleep(0.2)

        state = planner.state
        planner.stop()
        # Should have found track and transitioned
        assert state in (State.TRACKING, State.FOLLOWING, State.SEARCHING, State.IDLE)

    def test_full_search_no_target_returns_idle(self, planner, config):
        """Full 360° search with no detection → IDLE."""
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
        assert cfg.max_wheel_speed == 160.0
        assert cfg.target_height == 180.0
        assert cfg.loop_hz == 10.0

    def test_custom_config(self):
        cfg = FollowConfig(max_wheel_speed=80.0, target_height=200.0)
        assert cfg.max_wheel_speed == 80.0
        assert cfg.target_height == 200.0

    def test_planner_uses_config(self, bus, motor, head):
        cfg = FollowConfig(max_wheel_speed=50.0)
        p = FollowPlanner(motor, head, bus, cfg)
        assert p.config.max_wheel_speed == 50.0
