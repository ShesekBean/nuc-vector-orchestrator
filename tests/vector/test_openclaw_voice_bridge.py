"""Unit tests for OpenClawVoiceBridge — runs in CI without real hardware."""

from __future__ import annotations

import json
import struct
import threading
import time
from unittest.mock import MagicMock, patch


from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    STT_RESULT,
    TTS_PLAYING,
    WAKE_WORD_DETECTED,
    TtsPlayingEvent,
    WakeWordDetectedEvent,
)
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.openclaw_voice_bridge import (
    DEFAULT_HOOKS_URL,
    DEFAULT_SILENCE_THRESHOLD,
    BridgeState,
    OpenClawVoiceBridge,
    _pcm_to_wav,
    _rms_energy,
    _send_to_openclaw,
    _transcribe_openai,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm(n_samples: int = 1600, amplitude: int = 1000) -> bytes:
    """Generate PCM audio with a specific amplitude."""
    import math

    samples = [
        int(amplitude * math.sin(2 * math.pi * 440 * i / 16000))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def _make_silence(n_samples: int = 1600) -> bytes:
    """Generate silent PCM audio (near-zero amplitude)."""
    return _make_pcm(n_samples, amplitude=5)


def _mock_audio_client(chunks: list[bytes] | None = None):
    """Create a mock AudioClient that yields chunks sequentially."""
    client = MagicMock()
    if chunks is None:
        chunks = []

    call_count = [0]

    def _get_chunk():
        idx = call_count[0]
        if idx < len(chunks):
            return chunks[idx]
        return None

    def _chunk_count():
        return call_count[0]

    # chunk_count is a property on real AudioClient
    type(client).chunk_count = property(lambda self: call_count[0])

    def advance():
        call_count[0] += 1

    client.get_latest_chunk = _get_chunk
    client._advance = advance
    client._call_count = call_count
    return client


def _mock_robot():
    """Create a mock robot with say_text()."""
    robot = MagicMock()
    robot.behavior.say_text = MagicMock()
    return robot


# ---------------------------------------------------------------------------
# _rms_energy tests
# ---------------------------------------------------------------------------


class TestRmsEnergy:
    def test_silence(self):
        pcm = struct.pack("<4h", 0, 0, 0, 0)
        assert _rms_energy(pcm) == 0.0

    def test_nonzero(self):
        pcm = struct.pack("<4h", 100, -100, 100, -100)
        assert _rms_energy(pcm) == 100.0

    def test_empty(self):
        assert _rms_energy(b"") == 0.0

    def test_loud_audio(self):
        pcm = _make_pcm(1600, amplitude=10000)
        energy = _rms_energy(pcm)
        assert energy > 5000


# ---------------------------------------------------------------------------
# _pcm_to_wav tests
# ---------------------------------------------------------------------------


class TestPcmToWav:
    def test_valid_wav(self):
        pcm = _make_pcm(1600)
        wav_bytes = _pcm_to_wav(pcm)
        assert wav_bytes[:4] == b"RIFF"
        assert b"WAVE" in wav_bytes[:12]

    def test_wav_metadata(self):
        import io
        import wave

        pcm = _make_pcm(1600)
        wav_bytes = _pcm_to_wav(pcm, sample_rate=16000)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 1600

    def test_empty_pcm(self):
        wav_bytes = _pcm_to_wav(b"")
        assert wav_bytes[:4] == b"RIFF"


# ---------------------------------------------------------------------------
# _transcribe_openai tests
# ---------------------------------------------------------------------------


class TestTranscribeOpenai:
    def test_success(self):
        wav = _pcm_to_wav(_make_pcm(1600))
        response_data = json.dumps({"text": "hello world"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            return_value=mock_resp,
        ) as mock_urlopen:
            result = _transcribe_openai(wav, "sk-test-key")

        assert result == "hello world"
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-test-key"
        assert "multipart/form-data" in req.get_header("Content-type")

    def test_http_error(self):
        from urllib.error import HTTPError

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            side_effect=HTTPError(
                url="", code=429, msg="rate limited", hdrs={}, fp=None
            ),
        ):
            result = _transcribe_openai(b"wav-data", "sk-test-key")
        assert result == ""

    def test_connection_error(self):
        from urllib.error import URLError

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            side_effect=URLError("connection refused"),
        ):
            result = _transcribe_openai(b"wav-data", "sk-test-key")
        assert result == ""


# ---------------------------------------------------------------------------
# _send_to_openclaw tests
# ---------------------------------------------------------------------------


class TestSendToOpenclaw:
    def test_success(self):
        response_data = json.dumps({"response": "I see a person"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            return_value=mock_resp,
        ) as mock_urlopen:
            result = _send_to_openclaw(
                "what do you see", DEFAULT_HOOKS_URL, "test-token"
            )

        assert result == "I see a person"
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "Bearer test-token"
        body = json.loads(req.data.decode())
        assert body["message"] == "what do you see"
        assert body["deliver"] is False

    def test_text_field_fallback(self):
        """Response with 'text' key instead of 'response'."""
        response_data = json.dumps({"text": "fallback response"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            return_value=mock_resp,
        ):
            result = _send_to_openclaw("hello", DEFAULT_HOOKS_URL, "token")
        assert result == "fallback response"

    def test_http_error(self):
        from urllib.error import HTTPError

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            side_effect=HTTPError(
                url="", code=500, msg="server error", hdrs={}, fp=None
            ),
        ):
            result = _send_to_openclaw("hello", DEFAULT_HOOKS_URL, "token")
        assert result == ""


# ---------------------------------------------------------------------------
# BridgeState tests
# ---------------------------------------------------------------------------


class TestBridgeState:
    def test_states_exist(self):
        assert BridgeState.IDLE
        assert BridgeState.LISTENING
        assert BridgeState.TRANSCRIBING
        assert BridgeState.PROCESSING
        assert BridgeState.SPEAKING


# ---------------------------------------------------------------------------
# OpenClawVoiceBridge construction
# ---------------------------------------------------------------------------


class TestVoiceBridgeInit:
    def test_defaults(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )
        assert bridge.state == BridgeState.IDLE
        assert bridge.total_interactions == 0
        assert bridge.total_errors == 0
        assert bridge.last_latency_ms == 0.0

    def test_custom_config(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            hooks_url="http://custom:9999/hooks/agent",
            hooks_token="custom-token",
            openai_api_key="sk-custom",
            max_listen_sec=5.0,
            silence_threshold=500,
            silence_duration_sec=2.0,
            max_response_chars=200,
        )
        assert bridge._hooks_url == "http://custom:9999/hooks/agent"
        assert bridge._hooks_token == "custom-token"
        assert bridge._openai_api_key == "sk-custom"
        assert bridge._max_listen_sec == 5.0
        assert bridge._silence_threshold == 500
        assert bridge._silence_duration_sec == 2.0
        assert bridge._max_response_chars == 200


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_subscribes(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )
        bridge.start()
        assert bus.listener_count(WAKE_WORD_DETECTED) == 1

    def test_stop_unsubscribes(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )
        bridge.start()
        bridge.stop()
        assert bus.listener_count(WAKE_WORD_DETECTED) == 0


# ---------------------------------------------------------------------------
# Wake word handling — state transitions
# ---------------------------------------------------------------------------


class TestWakeWordHandling:
    def test_ignores_when_not_idle(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )
        bridge._state = BridgeState.PROCESSING

        event = WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=1.0, source="sdk"
        )
        bridge._on_wake_word(event)

        # Should still be PROCESSING, not LISTENING
        assert bridge.state == BridgeState.PROCESSING

    def test_transitions_to_listening(self):
        bus = NucEventBus()
        # Provide silence chunks so recording finishes quickly
        chunks = [_make_silence(1600)] * 20
        audio = _mock_audio_client(chunks)
        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_listen_sec=0.5,
            silence_duration_sec=0.1,
        )

        event = WakeWordDetectedEvent(
            model="hey_vector_sdk", confidence=1.0, source="sdk"
        )

        # Mock _record_speech to return empty (fast path to idle)
        bridge._record_speech = MagicMock(return_value=b"")
        bridge._on_wake_word(event)

        # Give thread time to start and finish
        time.sleep(0.3)
        assert bridge.state == BridgeState.IDLE
        assert bridge.total_interactions == 1


# ---------------------------------------------------------------------------
# Full interaction pipeline (mocked)
# ---------------------------------------------------------------------------


class TestInteractionPipeline:
    def test_full_pipeline(self):
        """Test the complete wake word → STT → agent → TTS flow."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()

        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            robot,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_response_chars=300,
        )

        # Track emitted events
        emitted: dict[str, list] = {
            STT_RESULT: [],
            TTS_PLAYING: [],
            COMMAND_RECEIVED: [],
        }
        bus.on(STT_RESULT, lambda e: emitted[STT_RESULT].append(e))
        bus.on(TTS_PLAYING, lambda e: emitted[TTS_PLAYING].append(e))
        bus.on(
            COMMAND_RECEIVED, lambda e: emitted[COMMAND_RECEIVED].append(e)
        )

        # Mock internal methods
        bridge._record_speech = MagicMock(return_value=_make_pcm(16000))
        bridge._transcribe = MagicMock(return_value="what is my balance")
        bridge._query_openclaw = MagicMock(
            return_value="Your balance is $1,234"
        )

        # Trigger interaction
        bridge._on_wake_word(
            WakeWordDetectedEvent(
                model="hey_vector_sdk", confidence=1.0, source="sdk"
            )
        )

        # Wait for thread
        time.sleep(0.5)

        assert bridge.state == BridgeState.IDLE
        assert bridge.total_interactions == 1
        assert bridge.total_errors == 0
        assert bridge.last_latency_ms > 0

        # Check STT event
        assert len(emitted[STT_RESULT]) == 1
        assert emitted[STT_RESULT][0].text == "what is my balance"

        # Check command event
        assert len(emitted[COMMAND_RECEIVED]) == 1
        assert emitted[COMMAND_RECEIVED][0].command == "what is my balance"
        assert emitted[COMMAND_RECEIVED][0].source == "voice"

        # Check TTS events (start + stop)
        assert len(emitted[TTS_PLAYING]) == 2
        assert emitted[TTS_PLAYING][0].playing is True
        assert emitted[TTS_PLAYING][1].playing is False

        # Check say_text called
        robot.behavior.say_text.assert_called_once_with(
            "Your balance is $1,234"
        )

    def test_stt_failure_returns_to_idle(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )

        bridge._record_speech = MagicMock(return_value=_make_pcm(1600))
        bridge._transcribe = MagicMock(return_value="")

        bridge._on_wake_word(
            WakeWordDetectedEvent(
                model="test", confidence=1.0, source="sdk"
            )
        )
        time.sleep(0.3)

        assert bridge.state == BridgeState.IDLE
        assert bridge.total_errors == 1

    def test_openclaw_failure_returns_to_idle(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )

        bridge._record_speech = MagicMock(return_value=_make_pcm(1600))
        bridge._transcribe = MagicMock(return_value="hello")
        bridge._query_openclaw = MagicMock(return_value="")

        bridge._on_wake_word(
            WakeWordDetectedEvent(
                model="test", confidence=1.0, source="sdk"
            )
        )
        time.sleep(0.3)

        assert bridge.state == BridgeState.IDLE
        assert bridge.total_errors == 1

    def test_no_speech_recorded(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token="tok"
        )

        bridge._record_speech = MagicMock(return_value=b"")
        bridge._on_wake_word(
            WakeWordDetectedEvent(
                model="test", confidence=1.0, source="sdk"
            )
        )
        time.sleep(0.3)

        assert bridge.state == BridgeState.IDLE
        assert bridge.total_errors == 1


# ---------------------------------------------------------------------------
# Response truncation
# ---------------------------------------------------------------------------


class TestResponseHandling:
    def test_long_response_passed_through(self):
        """Long responses are no longer truncated — SpeechOutput chunks them."""
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_response_chars=20,
        )

        long_response = "A" * 50
        response_data = json.dumps({"response": long_response}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            return_value=mock_resp,
        ):
            result = bridge._query_openclaw("test")

        # Full response returned — chunking handled by SpeechOutput.speak()
        assert len(result) == 50

    def test_short_response_returned_as_is(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_response_chars=300,
        )

        response_data = json.dumps({"response": "Hello!"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "apps.vector.src.voice.openclaw_voice_bridge.urlopen",
            return_value=mock_resp,
        ):
            result = bridge._query_openclaw("test")

        assert result == "Hello!"


# ---------------------------------------------------------------------------
# speak() TTS event emission
# ---------------------------------------------------------------------------


class TestSpeak:
    def test_emits_tts_events(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()
        bridge = OpenClawVoiceBridge(
            bus, audio, robot, openai_api_key="sk-test", hooks_token="tok"
        )

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        bridge._speak("Hello world")

        assert len(events) == 2
        assert events[0].playing is True
        assert events[0].text == "Hello world"
        assert events[1].playing is False
        robot.behavior.say_text.assert_called_once_with("Hello world")

    def test_no_robot_skips_tts(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, robot=None, openai_api_key="sk-test", hooks_token="tok"
        )
        # Should not raise
        bridge._speak("Hello")

    def test_say_text_failure_still_emits_stop(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        robot = _mock_robot()
        robot.behavior.say_text.side_effect = RuntimeError("TTS failed")

        bridge = OpenClawVoiceBridge(
            bus, audio, robot, openai_api_key="sk-test", hooks_token="tok"
        )

        events: list[TtsPlayingEvent] = []
        bus.on(TTS_PLAYING, lambda e: events.append(e))

        bridge._speak("Hello")

        # Both events emitted despite failure
        assert len(events) == 2
        assert events[0].playing is True
        assert events[1].playing is False


# ---------------------------------------------------------------------------
# _transcribe() with missing API key
# ---------------------------------------------------------------------------


class TestTranscribeMethod:
    def test_missing_api_key(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="", hooks_token="tok"
        )
        result = bridge._transcribe(_make_pcm(1600))
        assert result == ""

    def test_missing_hooks_token(self):
        bus = NucEventBus()
        audio = _mock_audio_client()
        bridge = OpenClawVoiceBridge(
            bus, audio, openai_api_key="sk-test", hooks_token=""
        )
        result = bridge._query_openclaw("test")
        assert result == ""


# ---------------------------------------------------------------------------
# Record speech with silence detection
# ---------------------------------------------------------------------------


class TestRecordSpeech:
    def test_silence_stops_recording(self):
        """Recording stops when silence is detected."""
        bus = NucEventBus()

        # 3 loud chunks then many silent chunks
        chunks = [_make_pcm(1600, amplitude=5000)] * 3
        chunks += [_make_silence(1600)] * 30

        audio = _mock_audio_client(chunks)
        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_listen_sec=5.0,
            silence_threshold=DEFAULT_SILENCE_THRESHOLD,
            silence_duration_sec=0.2,
        )

        # Simulate chunk advancement in a thread
        def advance_chunks():
            for _ in range(len(chunks)):
                audio._call_count[0] += 1
                time.sleep(0.05)

        t = threading.Thread(target=advance_chunks, daemon=True)
        t.start()

        pcm = bridge._record_speech()
        t.join(timeout=3.0)

        assert len(pcm) > 0
        # Should have stopped before consuming all chunks
        assert audio._call_count[0] <= len(chunks)

    def test_timeout_stops_recording(self):
        """Recording stops at max_listen_sec even without silence."""
        bus = NucEventBus()
        chunks = [_make_pcm(1600, amplitude=5000)] * 100
        audio = _mock_audio_client(chunks)

        bridge = OpenClawVoiceBridge(
            bus,
            audio,
            openai_api_key="sk-test",
            hooks_token="tok",
            max_listen_sec=0.3,
            silence_duration_sec=10.0,
        )

        def advance_chunks():
            for _ in range(100):
                audio._call_count[0] += 1
                time.sleep(0.02)

        t = threading.Thread(target=advance_chunks, daemon=True)
        t.start()

        start = time.monotonic()
        pcm = bridge._record_speech()
        elapsed = time.monotonic() - start
        t.join(timeout=3.0)

        assert len(pcm) > 0
        assert elapsed < 1.0  # should finish near 0.3s


# ---------------------------------------------------------------------------
# Hooks token loading
# ---------------------------------------------------------------------------


class TestLoadHooksToken:
    def test_from_file(self, tmp_path):
        token_file = tmp_path / "hooks-token"
        token_file.write_text("my-secret-token\n")

        with patch.dict(
            "os.environ",
            {"OPENCLAW_HOOKS_TOKEN_PATH": str(token_file)},
        ):
            token = OpenClawVoiceBridge._load_hooks_token()
        assert token == "my-secret-token"

    def test_missing_file_falls_to_env(self):
        with patch.dict(
            "os.environ",
            {
                "OPENCLAW_HOOKS_TOKEN_PATH": "/nonexistent/path",
                "OPENCLAW_HOOKS_TOKEN": "env-token",
            },
        ):
            token = OpenClawVoiceBridge._load_hooks_token()
        assert token == "env-token"

    def test_no_token_returns_empty(self):
        with patch.dict(
            "os.environ",
            {
                "OPENCLAW_HOOKS_TOKEN_PATH": "/nonexistent/path",
            },
            clear=False,
        ):
            # Remove OPENCLAW_HOOKS_TOKEN if set
            import os

            os.environ.pop("OPENCLAW_HOOKS_TOKEN", None)
            token = OpenClawVoiceBridge._load_hooks_token()
        assert token == ""
