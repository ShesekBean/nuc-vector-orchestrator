"""Tests for apps.vector.src.expression_engine — coordinated emotion expressions."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import (
    CLIFF_TRIGGERED,
    CliffTriggeredEvent,
    COMMAND_RECEIVED,
    CommandReceivedEvent,
    EXPRESSION_CHANGED,
    FACE_RECOGNIZED,
    FaceRecognizedEvent,
    TOUCH_DETECTED,
    TouchDetectedEvent,
    WAKE_WORD_DETECTED,
    WakeWordDetectedEvent,
    YOLO_PERSON_DETECTED,
    YoloPersonDetectedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.expression_engine import (
    EMOTIONS,
    ExpressionEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def bus():
    return NucEventBus()


@pytest.fixture()
def display():
    mock = MagicMock()
    mock.set_expression = MagicMock()
    return mock


@pytest.fixture()
def leds():
    mock = MagicMock()
    mock.override = MagicMock()
    mock._clear_override = MagicMock()
    return mock


@pytest.fixture()
def speech():
    mock = MagicMock()
    mock.speak = MagicMock()
    return mock


@pytest.fixture()
def engine(display, leds, speech, bus):
    eng = ExpressionEngine(display, leds, speech, bus)
    eng.start()
    yield eng
    eng.stop()


# ---------------------------------------------------------------------------
# Basic API
# ---------------------------------------------------------------------------

class TestExpressAPI:
    """Tests for the express() public API."""

    def test_initial_state_is_idle(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        assert eng.current_emotion == "idle"

    def test_express_happy(self, engine, display, leds, speech):
        engine.express("happy")
        assert engine.current_emotion == "happy"
        display.set_expression.assert_called_with("happy")
        leds.override.assert_called()
        speech.speak.assert_called_with("ha ha!")

    def test_express_curious(self, engine, display, leds, speech):
        engine.express("curious")
        assert engine.current_emotion == "curious"
        display.set_expression.assert_called_with("curious")
        speech.speak.assert_called_with("hmm?")

    def test_express_sad(self, engine, display, speech):
        engine.express("sad")
        assert engine.current_emotion == "sad"
        display.set_expression.assert_called_with("sad")
        speech.speak.assert_called_with("aww")

    def test_express_excited(self, engine, display, speech):
        engine.express("excited")
        assert engine.current_emotion == "excited"
        display.set_expression.assert_called_with("excited")
        speech.speak.assert_called_with("woo hoo!")

    def test_express_sleepy_no_sound(self, engine, display, speech):
        engine.express("sleepy")
        assert engine.current_emotion == "sleepy"
        display.set_expression.assert_called_with("sleepy")
        speech.speak.assert_not_called()

    def test_express_startled(self, engine, display, speech):
        engine.express("startled")
        assert engine.current_emotion == "startled"
        display.set_expression.assert_called_with("startled")
        speech.speak.assert_called_with("oh!")

    def test_express_idle_reverts_led(self, engine, display, leds):
        engine.express("happy")
        leds.override.reset_mock()
        engine.express("idle")
        assert engine.current_emotion == "idle"
        # Idle sets a very short override to let LedController's state take over
        leds.override.assert_called_once()
        _, kwargs = leds.override.call_args
        assert kwargs["duration_s"] < 0.1  # near-instant revert

    def test_express_unknown_raises(self, engine):
        with pytest.raises(ValueError, match="Unknown emotion"):
            engine.express("confused")

    def test_express_with_no_speech(self, display, leds, bus):
        eng = ExpressionEngine(display, leds, None, bus)
        eng.start()
        eng.express("happy")  # should not crash
        assert eng.current_emotion == "happy"
        eng.stop()


# ---------------------------------------------------------------------------
# Priority system
# ---------------------------------------------------------------------------

class TestPriority:
    """Tests for emotion priority resolution."""

    def test_higher_priority_overrides(self, engine, display):
        engine.express("curious")  # priority 3
        engine.express("startled")  # priority 6
        assert engine.current_emotion == "startled"
        display.set_expression.assert_called_with("startled")

    def test_lower_priority_suppressed(self, engine, display):
        engine.express("startled")  # priority 6
        display.set_expression.reset_mock()
        engine.express("curious")  # priority 3 — should be suppressed
        assert engine.current_emotion == "startled"
        display.set_expression.assert_not_called()

    def test_equal_priority_resets_timer(self, engine):
        engine.express("happy")
        engine.express("happy")  # same — should not crash, resets timer
        assert engine.current_emotion == "happy"

    def test_idle_always_accepted(self, engine):
        engine.express("startled")
        engine.express("idle")
        assert engine.current_emotion == "idle"


# ---------------------------------------------------------------------------
# Decay timer
# ---------------------------------------------------------------------------

class TestDecay:
    """Tests for auto-decay to idle."""

    def test_decay_to_idle(self, engine, display):
        engine.express("startled", duration_s=0.1)
        time.sleep(0.3)
        assert engine.current_emotion == "idle"

    def test_no_decay_with_zero_duration(self, engine):
        engine.express("happy", duration_s=0)
        time.sleep(0.2)
        assert engine.current_emotion == "happy"

    def test_decay_timer_cancelled_on_new_emotion(self, engine):
        engine.express("curious", duration_s=0.5)
        engine.express("startled", duration_s=0.1)
        # startled should decay, not curious
        time.sleep(0.3)
        assert engine.current_emotion == "idle"


# ---------------------------------------------------------------------------
# Event triggers
# ---------------------------------------------------------------------------

class TestEventTriggers:
    """Tests for event-driven emotion triggers."""

    def test_person_detected_triggers_curious(self, engine, bus):
        bus.emit(YOLO_PERSON_DETECTED, YoloPersonDetectedEvent(
            x=100, y=50, width=40, height=80, confidence=0.9,
        ))
        assert engine.current_emotion == "curious"

    def test_face_recognized_triggers_happy(self, engine, bus):
        bus.emit(FACE_RECOGNIZED, FaceRecognizedEvent(
            name="Ophir", confidence=0.9, x=100, y=50, width=40, height=40,
        ))
        assert engine.current_emotion == "happy"

    def test_touch_pressed_triggers_happy(self, engine, bus):
        bus.emit(TOUCH_DETECTED, TouchDetectedEvent(is_pressed=True))
        assert engine.current_emotion == "happy"

    def test_touch_released_does_not_trigger(self, engine, bus):
        bus.emit(TOUCH_DETECTED, TouchDetectedEvent(is_pressed=False))
        assert engine.current_emotion == "idle"

    def test_command_triggers_curious(self, engine, bus):
        bus.emit(COMMAND_RECEIVED, CommandReceivedEvent(
            command="dance", source="voice",
        ))
        assert engine.current_emotion == "curious"

    def test_cliff_triggers_startled(self, engine, bus):
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=0x01))
        assert engine.current_emotion == "startled"

    def test_wake_word_triggers_curious(self, engine, bus):
        bus.emit(WAKE_WORD_DETECTED, WakeWordDetectedEvent(
            model="hey_vector", confidence=0.9, source="sdk",
        ))
        assert engine.current_emotion == "curious"


# ---------------------------------------------------------------------------
# Expression change events
# ---------------------------------------------------------------------------

class TestExpressionChangedEvent:
    """Tests that EXPRESSION_CHANGED events are emitted correctly."""

    def test_emits_on_express(self, engine, bus):
        events = []
        bus.on(EXPRESSION_CHANGED, lambda e: events.append(e))
        engine.express("happy")
        assert len(events) == 1
        assert events[0].emotion == "happy"
        assert events[0].previous_emotion == "idle"
        assert events[0].trigger == "api"

    def test_emits_on_event_trigger(self, engine, bus):
        events = []
        bus.on(EXPRESSION_CHANGED, lambda e: events.append(e))
        bus.emit(CLIFF_TRIGGERED, CliffTriggeredEvent(cliff_flags=0x01))
        assert len(events) == 1
        assert events[0].emotion == "startled"
        assert events[0].trigger == "cliff"

    def test_emits_on_decay(self, engine, bus):
        events = []
        bus.on(EXPRESSION_CHANGED, lambda e: events.append(e))
        engine.express("startled", duration_s=0.1)
        time.sleep(0.3)
        assert len(events) == 2  # startled + idle decay
        assert events[1].emotion == "idle"
        assert events[1].trigger == "decay"

    def test_no_event_for_same_emotion(self, engine, bus):
        events = []
        engine.express("happy")
        bus.on(EXPRESSION_CHANGED, lambda e: events.append(e))
        engine.express("happy")  # same — no new event
        assert len(events) == 0


# ---------------------------------------------------------------------------
# LED integration
# ---------------------------------------------------------------------------

class TestLEDIntegration:
    """Tests for LED controller coordination."""

    def test_override_called_with_emotion_hue(self, engine, leds):
        engine.express("curious")
        defn = EMOTIONS["curious"]
        leds.override.assert_called_with(
            hue=defn.led_hue,
            saturation=defn.led_sat,
            duration_s=defn.decay_s,
        )

    def test_startled_uses_red(self, engine, leds):
        engine.express("startled")
        defn = EMOTIONS["startled"]
        leds.override.assert_called_with(
            hue=0.0,
            saturation=1.0,
            duration_s=defn.decay_s,
        )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_subscribes_to_events(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        eng.start()
        assert bus.listener_count(YOLO_PERSON_DETECTED) >= 1
        assert bus.listener_count(FACE_RECOGNIZED) >= 1
        assert bus.listener_count(TOUCH_DETECTED) >= 1
        assert bus.listener_count(COMMAND_RECEIVED) >= 1
        assert bus.listener_count(CLIFF_TRIGGERED) >= 1
        assert bus.listener_count(WAKE_WORD_DETECTED) >= 1
        eng.stop()

    def test_stop_unsubscribes(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        eng.start()
        eng.stop()
        assert bus.listener_count(YOLO_PERSON_DETECTED) == 0
        assert bus.listener_count(CLIFF_TRIGGERED) == 0

    def test_double_start_safe(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        eng.start()
        eng.start()  # should not double-subscribe
        assert bus.listener_count(YOLO_PERSON_DETECTED) == 1
        eng.stop()

    def test_double_stop_safe(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        eng.start()
        eng.stop()
        eng.stop()  # should not crash

    def test_stop_cancels_decay_timer(self, display, leds, speech, bus):
        eng = ExpressionEngine(display, leds, speech, bus)
        eng.start()
        eng.express("happy", duration_s=10.0)
        eng.stop()
        time.sleep(0.1)
        # Timer should be cancelled — emotion stays as-is
        assert eng.current_emotion == "happy"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Concurrent access tests."""

    def test_concurrent_express_calls(self, engine):
        errors = []

        def express_many(emotion, count):
            try:
                for _ in range(count):
                    engine.express(emotion)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=express_many, args=("happy", 50)),
            threading.Thread(target=express_many, args=("curious", 50)),
            threading.Thread(target=express_many, args=("startled", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert engine.current_emotion in EMOTIONS


# ---------------------------------------------------------------------------
# Speech error handling
# ---------------------------------------------------------------------------

class TestSpeechErrorHandling:
    """Tests that speech errors don't crash the engine."""

    def test_speech_error_does_not_crash(self, engine, speech):
        speech.speak.side_effect = RuntimeError("TTS failed")
        engine.express("happy")  # should not raise
        assert engine.current_emotion == "happy"


# ---------------------------------------------------------------------------
# All 6 emotions defined
# ---------------------------------------------------------------------------

class TestEmotionCoverage:
    """Verify all required emotions are defined."""

    REQUIRED = {"happy", "curious", "sad", "excited", "sleepy", "startled"}

    def test_all_required_emotions_defined(self):
        assert self.REQUIRED.issubset(set(EMOTIONS.keys()))

    def test_all_emotions_have_face_expression(self):
        from apps.vector.src.display_controller import EXPRESSIONS
        for name, defn in EMOTIONS.items():
            assert defn.face in EXPRESSIONS, (
                f"Emotion {name!r} uses face {defn.face!r} "
                f"which is not in EXPRESSIONS"
            )

    def test_all_emotions_have_valid_priority(self):
        for name, defn in EMOTIONS.items():
            assert isinstance(defn.priority, int)
            assert defn.priority >= 0

    def test_idle_has_no_decay(self):
        assert EMOTIONS["idle"].decay_s == 0

    def test_non_idle_have_positive_decay(self):
        for name, defn in EMOTIONS.items():
            if name != "idle":
                assert defn.decay_s > 0, f"{name} should have positive decay"
