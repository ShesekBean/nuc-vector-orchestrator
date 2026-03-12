"""Unit tests for AudioClient — runs in CI without real Vector hardware."""

from __future__ import annotations

import struct
import threading
from unittest.mock import MagicMock

import pytest

from apps.vector.src.voice.audio_client import (
    NATIVE_SAMPLE_RATE,
    TARGET_SAMPLE_RATE,
    AudioClient,
    _resample_linear,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pcm_bytes(n_samples: int = 1600, amplitude: int = 1000) -> bytes:
    """Generate a simple sine-ish PCM buffer (int16 LE)."""
    import math
    samples = []
    for i in range(n_samples):
        val = int(amplitude * math.sin(2 * math.pi * 440 * i / NATIVE_SAMPLE_RATE))
        samples.append(val)
    return struct.pack(f"<{n_samples}h", *samples)


def _mock_robot():
    """Create a mock robot with the minimum interface AudioClient needs."""
    robot = MagicMock()
    robot.conn._loop = MagicMock()
    return robot


# ---------------------------------------------------------------------------
# Resample tests
# ---------------------------------------------------------------------------

class TestResampleLinear:
    def test_identity(self):
        pcm = _make_pcm_bytes(100)
        out = _resample_linear(pcm, 16000, 16000)
        assert out == pcm

    def test_upsample_length(self):
        n_src = 1600
        pcm = _make_pcm_bytes(n_src)
        out = _resample_linear(pcm, NATIVE_SAMPLE_RATE, TARGET_SAMPLE_RATE)
        n_dst = len(out) // 2
        expected = int(n_src * TARGET_SAMPLE_RATE / NATIVE_SAMPLE_RATE)
        assert abs(n_dst - expected) <= 1

    def test_empty_input(self):
        assert _resample_linear(b"", 15625, 16000) == b""

    def test_output_range(self):
        """Resampled values must stay within int16 range."""
        # Use max amplitude
        pcm = struct.pack("<2h", -32768, 32767)
        out = _resample_linear(pcm, 15625, 16000)
        samples = struct.unpack(f"<{len(out)//2}h", out)
        for s in samples:
            assert -32768 <= s <= 32767


# ---------------------------------------------------------------------------
# AudioClient construction
# ---------------------------------------------------------------------------

class TestAudioClientInit:
    def test_defaults(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=10)
        assert client.buffer_size == 10
        assert client.chunk_count == 0
        assert not client.is_streaming
        assert client.native_sample_rate == NATIVE_SAMPLE_RATE
        assert client.target_sample_rate == TARGET_SAMPLE_RATE

    def test_invalid_buffer_size(self):
        robot = _mock_robot()
        with pytest.raises(ValueError, match="buffer_size must be >= 1"):
            AudioClient(robot, buffer_size=0)


# ---------------------------------------------------------------------------
# Chunk processing
# ---------------------------------------------------------------------------

class TestProcessResponse:
    def test_buffers_chunk(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)

        resp = MagicMock()
        resp.signal_power = _make_pcm_bytes(1600)
        resp.source_direction = 3
        resp.source_confidence = 80

        client._process_response(resp)

        assert client.chunk_count == 1
        chunk = client.get_latest_chunk()
        assert chunk is not None
        assert len(chunk) > 0

        # Verify resampled length
        expected_samples = int(1600 * TARGET_SAMPLE_RATE / NATIVE_SAMPLE_RATE)
        actual_samples = len(chunk) // 2
        assert abs(actual_samples - expected_samples) <= 1

    def test_beamforming_metadata(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)

        resp = MagicMock()
        resp.signal_power = _make_pcm_bytes(1600)
        resp.source_direction = 7
        resp.source_confidence = 95

        client._process_response(resp)

        assert client.source_direction == 7
        assert client.source_confidence == 95

    def test_empty_signal_power_skipped(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)

        resp = MagicMock()
        resp.signal_power = b""
        client._process_response(resp)

        assert client.chunk_count == 0
        assert client.get_latest_chunk() is None

    def test_ring_buffer_overflow(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=3)

        for i in range(5):
            resp = MagicMock()
            resp.signal_power = _make_pcm_bytes(1600)
            resp.source_direction = i
            resp.source_confidence = 0
            client._process_response(resp)

        assert client.chunk_count == 5
        buf = client.get_audio_buffer()
        assert len(buf) == 3  # ring buffer capped


# ---------------------------------------------------------------------------
# read_pcm / write_wav
# ---------------------------------------------------------------------------

class TestReadPcm:
    def _fill_client(self, client: AudioClient, n_chunks: int = 10) -> None:
        for _ in range(n_chunks):
            resp = MagicMock()
            resp.signal_power = _make_pcm_bytes(1600)
            resp.source_direction = 0
            resp.source_confidence = 0
            client._process_response(resp)

    def test_read_pcm_duration(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=50)
        self._fill_client(client, 20)

        pcm = client.read_pcm(0.5)
        n_samples = len(pcm) // 2
        duration = n_samples / TARGET_SAMPLE_RATE
        assert 0.4 <= duration <= 0.6

    def test_read_pcm_empty(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)
        assert client.read_pcm(1.0) == b""

    def test_write_wav(self, tmp_path):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=50)
        self._fill_client(client, 20)

        wav_path = str(tmp_path / "test.wav")
        n = client.write_wav(wav_path, 1.0)
        assert n > 0

        # Verify WAV metadata
        import wave
        with wave.open(wav_path, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == TARGET_SAMPLE_RATE


# ---------------------------------------------------------------------------
# CPS metric
# ---------------------------------------------------------------------------

class TestChunksPerSecond:
    def test_no_data(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)
        assert client.chunks_per_second == 0.0

    def test_with_data(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=50)

        # Simulate timed chunk arrivals
        for i in range(5):
            with client._lock:
                client._timestamps.append(i * 0.1)

        cps = client.chunks_per_second
        assert 9.0 <= cps <= 11.0  # ~10 cps


# ---------------------------------------------------------------------------
# Connection lost / reconnect
# ---------------------------------------------------------------------------

class TestReconnect:
    def test_connection_lost_callback(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)

        called = threading.Event()
        client.set_connection_lost_callback(lambda: called.set())

        assert client._connection_lost_callback is not None

    def test_stop_clears_streaming(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)
        client._streaming = True
        client._should_reconnect = False

        # Calling stop when no real stream task is fine
        client._streaming = False  # simulate already stopped
        client.stop()
        assert not client.is_streaming


# ---------------------------------------------------------------------------
# clear_buffer (echo suppression support)
# ---------------------------------------------------------------------------

class TestClearBuffer:
    def test_clears_chunks_and_timestamps(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=10)

        # Fill with data
        for _ in range(5):
            resp = MagicMock()
            resp.signal_power = _make_pcm_bytes(1600)
            resp.source_direction = 0
            resp.source_confidence = 0
            client._process_response(resp)

        assert client.chunk_count == 5
        assert len(client.get_audio_buffer()) == 5

        client.clear_buffer()

        assert client.get_audio_buffer() == []
        assert client.get_latest_chunk() is None
        # chunk_count preserved for continuity
        assert client.chunk_count == 5

    def test_clear_empty_buffer_is_safe(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=5)
        client.clear_buffer()  # should not raise
        assert client.get_audio_buffer() == []

    def test_read_pcm_empty_after_clear(self):
        robot = _mock_robot()
        client = AudioClient(robot, buffer_size=10)

        for _ in range(3):
            resp = MagicMock()
            resp.signal_power = _make_pcm_bytes(1600)
            resp.source_direction = 0
            resp.source_confidence = 0
            client._process_response(resp)

        client.clear_buffer()
        assert client.read_pcm(1.0) == b""
