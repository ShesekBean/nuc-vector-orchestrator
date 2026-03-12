"""Audio streaming client for Vector's 4-mic beamforming array.

Connects to Vector via the SDK's gRPC ``AudioFeed`` RPC and streams raw
PCM audio to NUC.  Chunks are decoded from the ``signal_power`` bytes
field (int16 LE at 15 625 Hz), resampled to 16 000 Hz mono, and stored
in a thread-safe ring buffer for downstream consumers (wake-word
detection, STT).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
import wave
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import anki_vector

logger = logging.getLogger(__name__)

# Vector's native mic sample rate (from SDK protocol constant)
NATIVE_SAMPLE_RATE = 15_625

# Target rate expected by openwakeword + OpenClaw Talk Mode STT
TARGET_SAMPLE_RATE = 16_000

# Rolling window size for chunks-per-second metric
_CPS_WINDOW = 30


def _resample_linear(pcm_int16: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM via linear interpolation.

    For the 15625→16000 conversion (2.4 % stretch) this is perfectly
    adequate — no heavy DSP library required.
    """
    if src_rate == dst_rate:
        return pcm_int16

    n_src = len(pcm_int16) // 2
    if n_src == 0:
        return b""

    src = struct.unpack(f"<{n_src}h", pcm_int16)
    ratio = src_rate / dst_rate
    n_dst = int(n_src / ratio)
    dst = []
    for i in range(n_dst):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        if idx + 1 < n_src:
            sample = src[idx] + frac * (src[idx + 1] - src[idx])
        else:
            sample = src[idx]
        # Clamp to int16 range
        sample = max(-32768, min(32767, int(round(sample))))
        dst.append(sample)
    return struct.pack(f"<{len(dst)}h", *dst)


class AudioClient:
    """Streams mic audio from Vector and buffers resampled PCM chunks.

    Args:
        robot: Connected ``anki_vector.Robot`` instance.
        buffer_size: Maximum number of resampled chunks to keep.
    """

    def __init__(self, robot: anki_vector.Robot, buffer_size: int = 50) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size must be >= 1")

        self._robot = robot
        self._buffer_size = buffer_size

        # Ring buffer of resampled PCM bytes (16 kHz, 16-bit, mono)
        self._chunks: deque[bytes] = deque(maxlen=buffer_size)
        # Timestamps for CPS (chunks-per-second) calculation
        self._timestamps: deque[float] = deque(maxlen=_CPS_WINDOW)

        self._chunk_count = 0
        self._streaming = False
        self._lock = threading.Lock()

        # Latest beamforming metadata
        self._source_direction: int = 0
        self._source_confidence: int = 0

        # Reconnection state
        self._reconnect_thread: threading.Thread | None = None
        self._should_reconnect = False
        self._max_reconnect_delay = 30.0
        self._connection_lost_callback: Callable[[], None] | None = None

        # Stream control
        self._stream_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the AudioFeed stream and begin buffering chunks."""
        if self._streaming:
            logger.warning("Audio feed already streaming")
            return

        self._should_reconnect = True
        self._start_feed()

    def stop(self) -> None:
        """Stop the audio stream and release resources."""
        self._should_reconnect = False
        self._stop_feed()

        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=5.0)
            self._reconnect_thread = None

    def get_latest_chunk(self) -> bytes | None:
        """Return the most recent resampled PCM chunk, or ``None``."""
        with self._lock:
            return self._chunks[-1] if self._chunks else None

    def get_audio_buffer(self) -> list[bytes]:
        """Return a copy of all buffered PCM chunks (oldest first)."""
        with self._lock:
            return list(self._chunks)

    def read_pcm(self, duration_sec: float) -> bytes:
        """Return up to *duration_sec* seconds of buffered PCM.

        Concatenates the most recent chunks that fit within the
        requested duration.  Returns ``b""`` if no data is available.
        """
        samples_needed = int(TARGET_SAMPLE_RATE * duration_sec)
        bytes_needed = samples_needed * 2  # 16-bit

        with self._lock:
            parts: list[bytes] = []
            total = 0
            for chunk in reversed(self._chunks):
                parts.append(chunk)
                total += len(chunk)
                if total >= bytes_needed:
                    break

        if not parts:
            return b""

        # Parts are newest-first; reverse to chronological order
        parts.reverse()
        pcm = b"".join(parts)

        # Trim to requested length from the end (most recent audio)
        if len(pcm) > bytes_needed:
            pcm = pcm[-bytes_needed:]
        return pcm

    def write_wav(self, path: str, duration_sec: float) -> int:
        """Write buffered audio to a WAV file.

        Returns the number of samples written.
        """
        pcm = self.read_pcm(duration_sec)
        n_samples = len(pcm) // 2
        if n_samples == 0:
            return 0

        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_SAMPLE_RATE)
            wf.writeframes(pcm)

        return n_samples

    def clear_buffer(self) -> None:
        """Discard all buffered audio chunks.

        Called by :class:`~apps.vector.src.voice.echo_cancel.EchoSuppressor`
        after TTS playback to remove echo-contaminated audio.  The stream
        continues running — only the ring buffer contents are discarded.
        ``chunk_count`` is preserved for continuity with consumers that
        track processed chunk IDs.
        """
        with self._lock:
            n = len(self._chunks)
            self._chunks.clear()
            self._timestamps.clear()
        logger.debug("Audio buffer cleared (%d chunks discarded)", n)

    def set_connection_lost_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the robot connection drops."""
        self._connection_lost_callback = callback

    @property
    def chunks_per_second(self) -> float:
        """Rolling chunks-per-second over the last window."""
        with self._lock:
            if len(self._timestamps) < 2:
                return 0.0
            elapsed = self._timestamps[-1] - self._timestamps[0]
            if elapsed <= 0:
                return 0.0
            return (len(self._timestamps) - 1) / elapsed

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def source_direction(self) -> int:
        """Most recent beamforming source direction (0–11 clock pos)."""
        return self._source_direction

    @property
    def source_confidence(self) -> int:
        """Confidence of the source direction estimate."""
        return self._source_confidence

    @property
    def native_sample_rate(self) -> int:
        return NATIVE_SAMPLE_RATE

    @property
    def target_sample_rate(self) -> int:
        return TARGET_SAMPLE_RATE

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_feed(self) -> None:
        """Launch the async AudioFeed consumer on the SDK event loop."""
        loop = self._robot.conn._loop

        self._stop_event = asyncio.Event()

        async def _create_task() -> None:
            self._stream_task = asyncio.ensure_future(self._consume_stream())

        future = asyncio.run_coroutine_threadsafe(_create_task(), loop)
        future.result(timeout=5.0)

        self._streaming = True
        logger.info("Audio feed started (buffer_size=%d)", self._buffer_size)

    def _stop_feed(self) -> None:
        """Cancel the stream consumer task."""
        if not self._streaming:
            return

        if self._stop_event is not None:
            loop = self._robot.conn._loop
            loop.call_soon_threadsafe(self._stop_event.set)

        if self._stream_task is not None:
            loop = self._robot.conn._loop
            loop.call_soon_threadsafe(self._stream_task.cancel)

        # Give the task a moment to clean up
        time.sleep(0.2)
        self._streaming = False
        self._stream_task = None
        logger.info("Audio feed stopped (total chunks: %d)", self._chunk_count)

    async def _consume_stream(self) -> None:
        """Async generator consumer — runs on the SDK event loop."""
        from anki_vector.messaging import protocol

        grpc_if = self._robot.conn.grpc_interface
        try:
            req = protocol.AudioFeedRequest()
            stream = grpc_if.AudioFeed(req)

            async for resp in stream:
                if self._stop_event and self._stop_event.is_set():
                    break

                self._process_response(resp)

        except asyncio.CancelledError:
            logger.debug("Audio stream task cancelled")
        except Exception:
            logger.exception("Audio stream error")
            self._streaming = False
            if self._should_reconnect:
                self._schedule_reconnect()

    def _process_response(self, resp: object) -> None:
        """Decode, resample, and buffer a single AudioFeedResponse."""
        now = time.monotonic()
        raw_pcm = resp.signal_power  # type: ignore[attr-defined]

        if not raw_pcm or len(raw_pcm) < 2:
            return

        # Resample 15625 → 16000 Hz
        resampled = _resample_linear(raw_pcm, NATIVE_SAMPLE_RATE, TARGET_SAMPLE_RATE)

        with self._lock:
            self._chunks.append(resampled)
            self._timestamps.append(now)
            self._chunk_count += 1

        # Update beamforming metadata (no lock needed — single writer)
        self._source_direction = resp.source_direction  # type: ignore[attr-defined]
        self._source_confidence = resp.source_confidence  # type: ignore[attr-defined]

    def _schedule_reconnect(self) -> None:
        """Spawn a background thread to reconnect with exponential backoff."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return

        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """Try to restart the audio feed with exponential backoff."""
        delay = 1.0
        while self._should_reconnect and not self._streaming:
            logger.info("Audio reconnect attempt in %.1fs...", delay)
            time.sleep(delay)
            if not self._should_reconnect:
                break
            try:
                self._start_feed()
                logger.info("Reconnected to audio feed")
                return
            except Exception:
                logger.warning("Audio reconnect failed, retrying...")
                delay = min(delay * 2, self._max_reconnect_delay)
