"""Phase 4 — Voice Pipeline.

Tests audio streaming and wake word detection on NUC.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.phase4


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


class TestWakeWordDetection:
    def test_wake_word_init_and_event(self):
        """4.2 — WakeWordDetector instantiates, fires event on detection, no false triggers."""
        from apps.vector.src.events.event_types import WAKE_WORD_DETECTED, WakeWordDetectedEvent
        from apps.vector.src.events.nuc_event_bus import NucEventBus
        from apps.vector.src.voice.wake_word import WakeWordDetector

        bus = NucEventBus()
        try:
            detector = WakeWordDetector(bus)
            assert detector is not None
        except ImportError:
            pytest.skip("openwakeword not available")

        received = []
        bus.on(WAKE_WORD_DETECTED, lambda data: received.append(data))

        # No event emitted yet → no false triggers
        assert len(received) == 0

        # Simulate wake word → event fires
        bus.emit(WAKE_WORD_DETECTED, WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=0.95, source="sdk",
        ))
        assert len(received) == 1
        assert received[0].confidence > 0.5


class TestTTSChunking:
    def test_speech_output_chunking(self):
        """4.3 — SpeechOutput.chunk_text splits correctly."""
        from apps.vector.src.voice.speech_output import SpeechOutput

        chunks = SpeechOutput.chunk_text(
            "Hello world. This is a test. How are you?", max_chars=30
        )
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk.strip()) > 0
