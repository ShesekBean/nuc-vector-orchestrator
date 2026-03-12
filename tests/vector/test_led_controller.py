"""Unit tests for LedController."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    EMERGENCY_STOP,
    LED_STATE_CHANGED,
    EmergencyStopEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.led_controller import (
    PATTERNS,
    PRIORITY,
    LedController,
)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def mock_robot():
    robot = MagicMock()
    robot.behavior.set_eye_color = MagicMock()
    return robot


@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def ctrl(mock_robot, bus):
    """Controller with default settings."""
    c = LedController(mock_robot, bus, override_duration_s=0.3)
    yield c
    # Ensure cleanup
    if c._running:
        c.stop()


# --- Lifecycle --------------------------------------------------------------


class TestLifecycle:
    def test_start_stop(self, ctrl, bus):
        ctrl.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1
        ctrl.stop()
        assert bus.listener_count(EMERGENCY_STOP) == 0

    def test_double_start_is_safe(self, ctrl, bus):
        ctrl.start()
        ctrl.start()
        assert bus.listener_count(EMERGENCY_STOP) == 1

    def test_double_stop_is_safe(self, ctrl):
        ctrl.start()
        ctrl.stop()
        ctrl.stop()  # should not raise

    def test_initial_state_is_idle(self, ctrl):
        ctrl.start()
        assert ctrl.current_state == "idle"

    def test_start_sets_eye_color(self, ctrl, mock_robot):
        ctrl.start()
        # Should set idle green
        mock_robot.behavior.set_eye_color.assert_called()
        args = mock_robot.behavior.set_eye_color.call_args[0]
        assert args == (0.33, 1.0)


# --- State management ------------------------------------------------------


class TestSetState:
    def test_set_known_state(self, ctrl):
        ctrl.start()
        ctrl.set_state("person_detected")
        assert ctrl.current_state == "person_detected"

    def test_unknown_state_raises(self, ctrl):
        ctrl.start()
        with pytest.raises(ValueError, match="Unknown LED state"):
            ctrl.set_state("nonexistent")

    def test_higher_priority_wins(self, ctrl):
        ctrl.start()
        ctrl.set_state("searching")
        ctrl.set_state("following")
        assert ctrl.current_state == "following"

    def test_lower_priority_ignored_while_higher_active(self, ctrl):
        ctrl.start()
        ctrl.set_state("low_battery")
        ctrl.set_state("searching")
        assert ctrl.current_state == "low_battery"

    def test_clear_state_falls_back(self, ctrl):
        ctrl.start()
        ctrl.set_state("person_detected")
        ctrl.set_state("following")
        ctrl.clear_state("following")
        assert ctrl.current_state == "person_detected"

    def test_clear_all_states_returns_to_idle(self, ctrl):
        ctrl.start()
        ctrl.set_state("searching")
        ctrl.clear_state("searching")
        assert ctrl.current_state == "idle"

    def test_clear_nonexistent_state_is_safe(self, ctrl):
        ctrl.start()
        ctrl.clear_state("following")  # not active — should not raise

    def test_active_states_tracking(self, ctrl):
        ctrl.start()
        ctrl.set_state("searching")
        ctrl.set_state("following")
        assert ctrl.active_states == frozenset({"searching", "following"})

    def test_battery_shutdown_highest_priority(self, ctrl):
        ctrl.start()
        ctrl.set_state("following")
        ctrl.set_state("low_battery")
        ctrl.set_state("battery_shutdown")
        assert ctrl.current_state == "battery_shutdown"


# --- Priority order ---------------------------------------------------------


class TestPriority:
    def test_all_states_have_patterns(self):
        for state in PRIORITY:
            assert state in PATTERNS

    def test_priority_order(self):
        assert PRIORITY["idle"] < PRIORITY["searching"]
        assert PRIORITY["searching"] < PRIORITY["person_detected"]
        assert PRIORITY["person_detected"] < PRIORITY["following"]
        assert PRIORITY["following"] < PRIORITY["low_battery"]
        assert PRIORITY["low_battery"] < PRIORITY["battery_shutdown"]


# --- Eye color output -------------------------------------------------------


class TestEyeColor:
    def test_idle_green(self, ctrl, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_eye_color.assert_called_with(0.33, 1.0)

    def test_person_detected_blue(self, ctrl, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_eye_color.reset_mock()
        ctrl.set_state("person_detected")
        mock_robot.behavior.set_eye_color.assert_called_with(0.67, 1.0)

    def test_battery_shutdown_red(self, ctrl, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_eye_color.reset_mock()
        ctrl.set_state("battery_shutdown")
        mock_robot.behavior.set_eye_color.assert_called_with(0.0, 1.0)

    def test_sdk_exception_does_not_crash(self, ctrl, mock_robot):
        mock_robot.behavior.set_eye_color.side_effect = RuntimeError("oops")
        ctrl.start()  # should not raise


# --- Override ---------------------------------------------------------------


class TestOverride:
    def test_override_sets_color(self, ctrl, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_eye_color.reset_mock()
        ctrl.override(hue=0.5, saturation=0.8)
        mock_robot.behavior.set_eye_color.assert_called_with(0.5, 0.8)

    def test_is_overridden(self, ctrl):
        ctrl.start()
        assert ctrl.is_overridden is False
        ctrl.override(hue=0.5, saturation=0.8)
        assert ctrl.is_overridden is True

    def test_override_auto_reverts(self, ctrl):
        ctrl.start()
        ctrl.override(hue=0.5, saturation=0.8, duration_s=0.2)
        assert ctrl.is_overridden is True
        time.sleep(0.5)
        assert ctrl.is_overridden is False

    def test_override_reverts_to_current_state(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.set_state("person_detected")
        mock_robot.behavior.set_eye_color.reset_mock()
        ctrl.override(hue=0.5, saturation=0.8, duration_s=0.2)
        time.sleep(0.5)
        # Should have reverted to person_detected blue
        last_call = mock_robot.behavior.set_eye_color.call_args[0]
        assert last_call == (0.67, 1.0)

    def test_cancel_override_on_stop(self, ctrl):
        ctrl.start()
        ctrl.override(hue=0.5, saturation=0.8, duration_s=10.0)
        ctrl.stop()
        # Timer should have been cancelled
        assert ctrl._override_timer is None


# --- Emergency stop ---------------------------------------------------------


class TestEmergencyStop:
    def test_e_stop_sets_red(self, ctrl, bus, mock_robot):
        ctrl.start()
        mock_robot.behavior.set_eye_color.reset_mock()
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="test"))
        # Animation loop should set red — give it time
        time.sleep(0.15)
        calls = mock_robot.behavior.set_eye_color.call_args_list
        # At least one call with red (0.0, 1.0)
        red_calls = [c for c in calls if c[0] == (0.0, 1.0)]
        assert len(red_calls) > 0

    def test_clear_e_stop_restores_state(self, ctrl, bus):
        ctrl.start()
        ctrl.set_state("person_detected")
        bus.emit(EMERGENCY_STOP, EmergencyStopEvent(source="test"))
        ctrl.clear_emergency_stop()
        assert ctrl.current_state == "person_detected"


# --- Event emission ---------------------------------------------------------


class TestEvents:
    def test_state_change_emits_event(self, ctrl, bus):
        received = []
        bus.on(LED_STATE_CHANGED, lambda e: received.append(e))
        ctrl.start()
        # Start emits idle
        ctrl.set_state("searching")
        assert len(received) >= 2
        last = received[-1]
        assert last.state == "searching"
        assert last.previous_state == "idle"

    def test_no_event_on_same_state(self, ctrl, bus):
        ctrl.start()
        received = []
        bus.on(LED_STATE_CHANGED, lambda e: received.append(e))
        ctrl.set_state("person_detected")
        count_after_first = len(received)
        ctrl.set_state("person_detected")  # same state again
        assert len(received) == count_after_first


# --- Animated patterns (integration) ----------------------------------------


class TestAnimatedPatterns:
    def test_breathing_varies_saturation(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.set_state("following")
        mock_robot.behavior.set_eye_color.reset_mock()
        # Let animation run briefly
        time.sleep(0.3)
        calls = mock_robot.behavior.set_eye_color.call_args_list
        assert len(calls) > 1
        # All calls should use blue hue (0.67)
        hues = {round(c[0][0], 2) for c in calls}
        assert hues == {0.67}
        # Saturation should vary
        sats = {round(c[0][1], 2) for c in calls}
        assert len(sats) > 1

    def test_blinking_alternates(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.set_state("low_battery")
        mock_robot.behavior.set_eye_color.reset_mock()
        time.sleep(0.5)
        calls = mock_robot.behavior.set_eye_color.call_args_list
        assert len(calls) > 1
        sats = [c[0][1] for c in calls]
        # Should have both on (1.0) and off (0.0) saturation values
        assert any(s == pytest.approx(1.0) for s in sats)
        assert any(s == pytest.approx(0.0) for s in sats)

    def test_cycling_varies_hue(self, ctrl, mock_robot):
        ctrl.start()
        ctrl.set_state("searching")
        mock_robot.behavior.set_eye_color.reset_mock()
        time.sleep(0.3)
        calls = mock_robot.behavior.set_eye_color.call_args_list
        assert len(calls) > 1
        hues = {round(c[0][0], 2) for c in calls}
        assert len(hues) > 1


# --- Thread safety ----------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_state_changes(self, ctrl):
        ctrl.start()
        errors = []
        states = list(PRIORITY.keys())

        def changer(state):
            try:
                for _ in range(20):
                    ctrl.set_state(state)
                    ctrl.clear_state(state)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=changer, args=(s,)) for s in states]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
