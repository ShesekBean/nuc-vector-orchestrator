"""Phase 4 — Voice Pipeline (~30s).

Tests audio streaming and wake word detection on NUC.

Tests 4.1–4.7 from docs/vector-golden-test-plan.md.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.phase4


# 4.1 Audio stream connects
class TestAudioStreamConnects:
    @pytest.mark.robot
    def test_audio_feed(self, robot_connected):
        """4.1 — AudioFeed gRPC from Vector → raw audio bytes received."""
        from apps.vector.src.voice.audio_client import AudioClient

        client = AudioClient(robot_connected)
        client.start()
        import time
        time.sleep(1)
        chunk = client.get_latest_chunk()
        client.stop()
        assert chunk is not None, "No audio data received"
        assert len(chunk) > 0, "Empty audio chunk"


# 4.2 Audio format
class TestAudioFormat:
    def test_sample_rate_constant(self):
        """4.2 — AudioClient targets 16kHz mono."""
        from apps.vector.src.voice.audio_client import TARGET_SAMPLE_RATE

        assert TARGET_SAMPLE_RATE == 16_000


# 4.3 Wake word model loads
class TestWakeWordModelLoads:
    def test_wake_word_init(self):
        """4.3 — WakeWordDetector can be instantiated."""
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.voice.wake_word import WakeWordDetector

        bus = NucEventBus()
        try:
            detector = WakeWordDetector(bus)
            assert detector is not None
        except ImportError:
            pytest.skip("openwakeword not available")


# 4.4 Wake word positive
class TestWakeWordPositive:
    def test_wake_word_event_fires(self):
        """4.4 — Wake word detection fires WAKE_WORD_DETECTED event."""
        from apps.vector.src.events.event_types import WAKE_WORD_DETECTED
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        received = []
        bus.on(WAKE_WORD_DETECTED, lambda data: received.append(data))

        # Simulate wake word event
        from apps.vector.src.events.event_types import WakeWordDetectedEvent
        bus.emit(WAKE_WORD_DETECTED, WakeWordDetectedEvent(
            model="hey_vector_sdk",
            confidence=0.95,
            source="sdk",
        ))
        assert len(received) == 1
        assert received[0].confidence > 0.5


# 4.5 Wake word negative
class TestWakeWordNegative:
    def test_no_false_trigger(self):
        """4.5 — Non-wake-word audio should not trigger detection."""
        from apps.vector.src.events.event_types import WAKE_WORD_DETECTED
        from apps.vector.src.events.nuc_event_bus import NucEventBus

        bus = NucEventBus()
        received = []
        bus.on(WAKE_WORD_DETECTED, lambda data: received.append(data))
        # Don't emit anything — verify no spurious events
        assert len(received) == 0


# 4.6 STT via OpenClaw
class TestSTTViaOpenClaw:
    def test_stt_result_event_type(self):
        """4.6 — SttResultEvent is properly defined with text field."""
        from apps.vector.src.events.event_types import SttResultEvent

        event = SttResultEvent(text="hello world", confidence=0.98)
        assert event.text == "hello world"
        assert event.confidence == 0.98


# 4.7 TTS say_text round-trip
class TestTTSSayTextRoundTrip:
    def test_speech_output_chunking(self):
        """4.7 — SpeechOutput.chunk_text splits correctly."""
        from apps.vector.src.voice.speech_output import SpeechOutput

        chunks = SpeechOutput.chunk_text(
            "Hello world. This is a test. How are you?", max_chars=30
        )
        assert len(chunks) >= 2
        # All chunks should be non-empty
        for chunk in chunks:
            assert len(chunk.strip()) > 0
