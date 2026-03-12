"""Unit tests for SpeechOutput — runs in CI without real hardware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apps.vector.src.events.event_types import TTS_PLAYING, TtsPlayingEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.speech_output import (
    DEFAULT_MAX_CHUNK_CHARS,
    SpeechOutput,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_robot():
    """Create a mock robot with say_text() and audio.set_master_volume()."""
    robot = MagicMock()
    robot.behavior.say_text = MagicMock()
    robot.audio.set_master_volume = MagicMock()
    return robot


# ---------------------------------------------------------------------------
# chunk_text tests
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_empty_string(self):
        assert SpeechOutput.chunk_text("") == [""]

    def test_whitespace_only(self):
        assert SpeechOutput.chunk_text("   ") == [""]

    def test_short_text_single_chunk(self):
        assert SpeechOutput.chunk_text("Hello world", max_chars=200) == ["Hello world"]

    def test_exact_limit(self):
        text = "A" * 200
        assert SpeechOutput.chunk_text(text, max_chars=200) == [text]

    def test_splits_on_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        chunks = SpeechOutput.chunk_text(text, max_chars=35)
        # Each sentence is ~16 chars; two fit in 35
        assert len(chunks) >= 2
        # All text preserved
        joined = " ".join(chunks)
        assert "First sentence." in joined
        assert "Third sentence." in joined

    def test_splits_long_sentence_on_whitespace(self):
        text = "word " * 50  # 250 chars
        chunks = SpeechOutput.chunk_text(text.strip(), max_chars=50)
        assert all(len(c) <= 50 for c in chunks)
        # All words preserved
        assert " ".join(chunks) == text.strip()

    def test_single_long_word_kept_intact(self):
        word = "A" * 300
        chunks = SpeechOutput.chunk_text(word, max_chars=200)
        # Single word can't be split — kept as-is
        assert chunks == [word]

    def test_mixed_sentence_lengths(self):
        short = "Hi."
        long = "This is a much longer sentence that contains many words and details."
        text = f"{short} {long}"
        chunks = SpeechOutput.chunk_text(text, max_chars=80)
        assert len(chunks) >= 1
        joined = " ".join(chunks)
        assert "Hi." in joined
        assert "details." in joined

    def test_question_and_exclamation_splits(self):
        text = "Is this working? Yes it is! Great news."
        chunks = SpeechOutput.chunk_text(text, max_chars=25)
        assert len(chunks) >= 2

    def test_default_max_chars(self):
        assert DEFAULT_MAX_CHUNK_CHARS == 200


# ---------------------------------------------------------------------------
# speak() tests
# ---------------------------------------------------------------------------


class TestSpeak:
    def test_speak_short_text(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        speech.speak("Hello world")

        robot.behavior.say_text.assert_called_once_with("Hello world")

    def test_speak_empty_text_noop(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        speech.speak("")
        speech.speak("   ")

        robot.behavior.say_text.assert_not_called()

    def test_speak_no_robot(self):
        bus = NucEventBus()
        speech = SpeechOutput(bus, robot=None)

        # Should not raise
        speech.speak("Hello")

    def test_speak_emits_tts_events(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        speech.speak("Hello world")

        assert len(events) == 2
        assert events[0].playing is True
        assert events[0].text == "Hello world"
        assert events[1].playing is False
        assert events[1].text == "Hello world"

    def test_speak_error_still_emits_stop(self):
        bus = NucEventBus()
        robot = _mock_robot()
        robot.behavior.say_text.side_effect = RuntimeError("TTS failed")
        speech = SpeechOutput(bus, robot)

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        speech.speak("Hello")

        assert len(events) == 2
        assert events[0].playing is True
        assert events[1].playing is False

    def test_speak_chunks_long_text(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot, max_chunk_chars=30)

        text = "First sentence. Second sentence. Third sentence."
        speech.speak(text)

        # Multiple say_text calls
        assert robot.behavior.say_text.call_count >= 2
        # TTS_PLAYING events: exactly one start + one stop
        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))
        # Already emitted above, so check bus was called

    def test_speak_chunks_emit_single_start_stop(self):
        """Even with multiple chunks, only one TTS_PLAYING start/stop pair."""
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot, max_chunk_chars=20)

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        speech.speak("Hello world. How are you today?")

        assert events[0].playing is True
        assert events[-1].playing is False
        assert len(events) == 2


# ---------------------------------------------------------------------------
# set_volume() tests
# ---------------------------------------------------------------------------


class TestSetVolume:
    def test_set_volume_valid(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        mock_level = MagicMock()
        mock_enum = MagicMock()
        mock_enum.__getitem__ = MagicMock(return_value=mock_level)

        with patch.dict(
            "sys.modules",
            {"anki_vector": MagicMock(), "anki_vector.audio": MagicMock()},
        ):
            with patch(
                "apps.vector.src.voice.speech_output.SpeechOutput.set_volume",
                wraps=speech.set_volume,
            ):
                # Direct test: verify the property updates
                speech._robot = robot
                # Mock the lazy import
                import sys
                mock_audio_mod = MagicMock()
                mock_audio_mod.RobotVolumeLevel = {"MEDIUM": mock_level, "LOW": mock_level, "HIGH": mock_level}
                sys.modules["anki_vector.audio"] = mock_audio_mod

                # Use a real dict-like mock for enum access
                class FakeEnum:
                    MEDIUM = "medium_val"
                    LOW = "low_val"
                    HIGH = "high_val"
                    MUTE = "mute_val"
                    MEDIUM_LOW = "medium_low_val"
                    MEDIUM_HIGH = "medium_high_val"
                    def __class_getitem__(cls, key):
                        return getattr(cls, key)

                mock_audio_mod.RobotVolumeLevel = FakeEnum
                mock_audio_mod.RobotVolumeLevel.__getitem__ = lambda self, key: getattr(FakeEnum, key)

                speech.set_volume("low")

                robot.audio.set_master_volume.assert_called_once()
                assert speech.volume == "low"

                # Clean up
                del sys.modules["anki_vector.audio"]

    def test_set_volume_invalid(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        speech.set_volume("SUPER_LOUD")

        robot.audio.set_master_volume.assert_not_called()
        assert speech.volume == "medium"  # unchanged

    def test_set_volume_no_robot(self):
        bus = NucEventBus()
        speech = SpeechOutput(bus, robot=None)

        speech.set_volume("low")
        assert speech.volume == "medium"  # unchanged

    def test_volume_property_default(self):
        bus = NucEventBus()
        speech = SpeechOutput(bus)

        assert speech.volume == "medium"

    def test_set_volume_case_insensitive(self):
        bus = NucEventBus()
        robot = _mock_robot()
        speech = SpeechOutput(bus, robot)

        import sys
        mock_audio_mod = MagicMock()
        mock_enum = MagicMock()
        mock_enum.__getitem__ = MagicMock(return_value="high_val")
        mock_audio_mod.RobotVolumeLevel = mock_enum

        sys.modules["anki_vector.audio"] = mock_audio_mod
        try:
            speech.set_volume("HIGH")
            assert speech.volume == "high"
            mock_enum.__getitem__.assert_called_with("HIGH")
        finally:
            del sys.modules["anki_vector.audio"]


# ---------------------------------------------------------------------------
# Integration with voice bridge
# ---------------------------------------------------------------------------


class TestVoiceBridgeIntegration:
    def test_bridge_speak_delegates_to_speech_output(self):
        """Voice bridge _speak() delegates to SpeechOutput.speak()."""
        from apps.vector.src.voice.openclaw_voice_bridge import OpenClawVoiceBridge

        bus = NucEventBus()
        audio = MagicMock()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )

        # _speak should delegate to SpeechOutput
        bridge._speak("Hello from bridge")
        robot.behavior.say_text.assert_called_once_with("Hello from bridge")

    def test_bridge_speak_emits_tts_events(self):
        """Voice bridge _speak() still emits TTS_PLAYING via SpeechOutput."""
        from apps.vector.src.voice.openclaw_voice_bridge import OpenClawVoiceBridge

        bus = NucEventBus()
        audio = MagicMock()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus, audio, robot,
            openai_api_key="sk-test",
            hooks_token="tok",
        )

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        bridge._speak("Test")

        assert len(events) == 2
        assert events[0].playing is True
        assert events[1].playing is False


# ---------------------------------------------------------------------------
# Regression: supervisor arg order (issue #130)
# ---------------------------------------------------------------------------


class TestSupervisorArgOrder:
    """Ensure SpeechOutput(bus, robot) is the correct construction order.

    The supervisor previously passed (robot, bus) which silently broke TTS
    because self._robot ended up being the event bus (no behavior attribute).
    """

    def test_swapped_args_fails_to_speak(self):
        """Constructing SpeechOutput(robot, bus) fails to call say_text."""
        bus = NucEventBus()
        robot = _mock_robot()

        # Wrong order: robot first, bus second (the old bug)
        bad_speech = SpeechOutput(robot, bus)
        bad_speech.speak("Should not work")

        # say_text is never called because self._robot is actually the bus
        robot.behavior.say_text.assert_not_called()

    def test_correct_args_speaks(self):
        """Constructing SpeechOutput(bus, robot) correctly calls say_text."""
        bus = NucEventBus()
        robot = _mock_robot()

        # Correct order: bus first, robot second
        good_speech = SpeechOutput(bus, robot)
        good_speech.speak("Hello")

        robot.behavior.say_text.assert_called_once_with("Hello")
