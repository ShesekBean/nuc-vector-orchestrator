"""Unit tests for WakeWordDetector — runs in CI without real hardware."""

from __future__ import annotations

import struct
import time
from unittest.mock import MagicMock, patch

import pytest

from apps.vector.src.events.event_types import (
    TTS_PLAYING,
    WAKE_WORD_DETECTED,
    TtsPlayingEvent,
    WakeWordDetectedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.wake_word import (
    DEFAULT_THRESHOLD,
    OWW_CHUNK_SAMPLES,
    WakeWordDetector,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_int16_pcm(n_samples: int = 1280) -> bytes:
    """Generate silent int16 PCM bytes."""
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _mock_oww_model(model_name: str = "hey_jarvis_v0.1", score: float = 0.0):
    """Create a mock openwakeword Model."""
    model = MagicMock()
    model.prediction_buffer = {model_name: []}
    model.predict.return_value = {model_name: score}
    return model


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestWakeWordDetectorInit:
    def test_defaults(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus)
        assert detector.threshold == DEFAULT_THRESHOLD
        assert not detector.sdk_active
        assert not detector.oww_active
        assert not detector.is_suppressed
        detector.stop()

    def test_custom_threshold(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, threshold=0.6)
        assert detector.threshold == 0.6
        detector.stop()

    def test_threshold_setter(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus)
        detector.threshold = 0.8
        assert detector.threshold == 0.8
        detector.stop()


# ---------------------------------------------------------------------------
# SDK backend
# ---------------------------------------------------------------------------

class TestSdkWakeWord:
    def test_sdk_listener_starts(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus)

        robot = MagicMock()
        # Patch Events import inside start_sdk_listener
        mock_events = MagicMock()
        mock_events.wake_word = "wake_word"
        with patch.dict("sys.modules", {"anki_vector.events": MagicMock(Events=mock_events)}):
            detector.start_sdk_listener(robot)

        assert detector.sdk_active
        robot.events.subscribe.assert_called_once()
        detector.stop()

    def test_sdk_detection_emits_event(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=0.0)

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        # Simulate SDK wake word callback
        detector._on_sdk_wake_word(None, "wake_word", None)

        assert len(events_received) == 1
        assert events_received[0].model == "hey_vector_sdk"
        assert events_received[0].confidence == 1.0
        assert events_received[0].source == "sdk"
        detector.stop()

    def test_sdk_no_duplicate_start(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus)

        robot = MagicMock()
        mock_events = MagicMock()
        mock_events.wake_word = "wake_word"
        with patch.dict("sys.modules", {"anki_vector.events": MagicMock(Events=mock_events)}):
            detector.start_sdk_listener(robot)
            detector.start_sdk_listener(robot)  # should warn, not double-subscribe

        assert robot.events.subscribe.call_count == 1
        detector.stop()


# ---------------------------------------------------------------------------
# openwakeword backend
# ---------------------------------------------------------------------------

class TestOwwBackend:
    def test_process_frame_below_threshold(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, threshold=0.5)

        mock_model = _mock_oww_model(score=0.3)
        detector._oww_model = mock_model
        detector._oww_model_names = ["hey_jarvis_v0.1"]

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        pcm = _make_int16_pcm(OWW_CHUNK_SAMPLES)
        detector._process_oww_frame(pcm)

        assert len(events_received) == 0

    def test_process_frame_above_threshold(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, threshold=0.4, cooldown_sec=0.0)

        mock_model = _mock_oww_model(score=0.85)
        detector._oww_model = mock_model
        detector._oww_model_names = ["hey_jarvis_v0.1"]

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        pcm = _make_int16_pcm(OWW_CHUNK_SAMPLES)
        detector._process_oww_frame(pcm)

        assert len(events_received) == 1
        evt = events_received[0]
        assert evt.model == "hey_jarvis_v0.1"
        assert evt.confidence == 0.85
        assert evt.source == "openwakeword"
        mock_model.reset.assert_called_once()

    def test_int16_input_enforced(self):
        """Verify openwakeword receives int16 tuple, not float32."""
        bus = NucEventBus()
        detector = WakeWordDetector(bus, threshold=0.4, cooldown_sec=0.0)

        mock_model = _mock_oww_model(score=0.1)
        detector._oww_model = mock_model
        detector._oww_model_names = ["hey_jarvis_v0.1"]

        pcm = _make_int16_pcm(OWW_CHUNK_SAMPLES)
        detector._process_oww_frame(pcm)

        # Verify predict was called with int16 tuple (not float32)
        call_args = mock_model.predict.call_args[0][0]
        assert isinstance(call_args, tuple)
        for sample in call_args:
            assert isinstance(sample, int)

    def test_oww_with_source_direction(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, threshold=0.4, cooldown_sec=0.0)

        mock_model = _mock_oww_model(score=0.9)
        detector._oww_model = mock_model
        detector._oww_model_names = ["hey_jarvis_v0.1"]

        # Mock audio client with source direction
        mock_client = MagicMock()
        mock_client.source_direction = 7
        detector._audio_client = mock_client

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        pcm = _make_int16_pcm(OWW_CHUNK_SAMPLES)
        detector._process_oww_frame(pcm)

        assert len(events_received) == 1
        assert events_received[0].source_direction == 7


# ---------------------------------------------------------------------------
# Suppression and cooldown
# ---------------------------------------------------------------------------

class TestSuppression:
    def test_suppress_for_blocks_detection(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=0.0)

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        detector.suppress_for(10.0)
        assert detector.is_suppressed

        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 0

    def test_suppression_expires(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=0.0)

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        detector.suppress_for(0.01)
        time.sleep(0.05)
        assert not detector.is_suppressed

        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 1

    def test_cooldown_blocks_rapid_detections(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=1.0)

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        # First detection should pass
        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 1

        # Second immediate detection should be blocked by cooldown
        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 1

    def test_tts_playing_suppresses(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=0.0)

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))

        # Simulate TTS start
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hello"))
        assert detector.is_suppressed

        # Detection should be blocked
        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 0

    def test_tts_stopped_holdoff(self):
        bus = NucEventBus()
        detector = WakeWordDetector(
            bus, cooldown_sec=0.0, tts_holdoff_sec=0.05
        )

        # Simulate TTS start then stop
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hi"))
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="hi"))

        # Should still be suppressed during holdoff
        assert detector.is_suppressed

        # After holdoff expires, detection should work
        time.sleep(0.1)
        assert not detector.is_suppressed

        events_received: list[WakeWordDetectedEvent] = []
        bus.on(WAKE_WORD_DETECTED, lambda e: events_received.append(e))
        detector._on_sdk_wake_word(None, "wake_word", None)
        assert len(events_received) == 1
        detector.stop()

    def test_tts_none_event_ignored(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus, cooldown_sec=0.0)

        # Should not crash on None event
        detector._on_tts_event(None)
        assert not detector.is_suppressed
        detector.stop()


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------

class TestWakeWordDetectedEvent:
    def test_frozen(self):
        evt = WakeWordDetectedEvent(
            model="hey_vector_sdk",
            confidence=1.0,
            source="sdk",
        )
        with pytest.raises(AttributeError):
            evt.model = "changed"  # type: ignore[misc]

    def test_default_direction(self):
        evt = WakeWordDetectedEvent(
            model="test", confidence=0.5, source="openwakeword"
        )
        assert evt.source_direction == -1

    def test_custom_direction(self):
        evt = WakeWordDetectedEvent(
            model="test", confidence=0.5, source="openwakeword",
            source_direction=3,
        )
        assert evt.source_direction == 3


# ---------------------------------------------------------------------------
# Stop / teardown
# ---------------------------------------------------------------------------

class TestStopTeardown:
    def test_stop_without_start(self):
        """Stop should be safe to call even if nothing was started."""
        bus = NucEventBus()
        detector = WakeWordDetector(bus)
        detector.stop()  # should not raise

    def test_stop_cleans_sdk(self):
        bus = NucEventBus()
        detector = WakeWordDetector(bus)

        robot = MagicMock()
        mock_events = MagicMock()
        mock_events.wake_word = "wake_word"
        with patch.dict("sys.modules", {"anki_vector.events": MagicMock(Events=mock_events)}):
            detector.start_sdk_listener(robot)

        assert detector.sdk_active
        with patch.dict("sys.modules", {"anki_vector.events": MagicMock(Events=mock_events)}):
            detector.stop()
        assert not detector.sdk_active
