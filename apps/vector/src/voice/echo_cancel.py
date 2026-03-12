"""Echo suppression for Vector's speaker-to-mic feedback loop.

Prevents say_text() audio from being picked up by Vector's own microphone
and corrupting wake word detection or STT input.  Speaker and mic are
inches apart in the same device, so temporal suppression is essential.

Strategy (from issue #37):
    say_text() is a **blocking** SDK call.  SpeechOutput emits
    ``TTS_PLAYING(playing=True)`` before the first chunk and
    ``TTS_PLAYING(playing=False)`` after the last.  This module listens
    for those events, marks a suppression window, and flushes the
    AudioClient ring buffer when TTS stops so that echo-contaminated
    audio is never fed to STT.

    Wake word suppression is handled independently by WakeWordDetector
    (which also subscribes to TTS_PLAYING).  This module covers the
    *audio buffer* side — ensuring stale echo audio doesn't leak into
    the next voice interaction.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import TTS_PLAYING

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.voice.audio_client import AudioClient

logger = logging.getLogger(__name__)

# Default hold-off after TTS stops (seconds).  Allows reverb / echo tail
# to decay before resuming mic processing.
DEFAULT_HOLDOFF_SEC = 0.5


class EchoSuppressor:
    """Centralized echo suppression coordinator.

    Subscribes to ``TTS_PLAYING`` events on the NUC event bus and:

    1. Tracks whether suppression is currently active (during TTS +
       hold-off period after TTS stops).
    2. Flushes the ``AudioClient`` ring buffer when TTS stops so that
       echo-contaminated audio is discarded before the next recording.

    Args:
        nuc_bus: NUC event bus for ``TTS_PLAYING`` subscription.
        audio_client: AudioClient whose buffer is flushed on TTS stop.
            May be ``None`` if buffer flushing is not needed.
        holdoff_sec: Seconds to remain suppressed after TTS stops.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        audio_client: AudioClient | None = None,
        *,
        holdoff_sec: float = DEFAULT_HOLDOFF_SEC,
    ) -> None:
        self._bus = nuc_bus
        self._audio_client = audio_client
        self._holdoff_sec = holdoff_sec

        # Suppression state — monotonic timestamp until which we suppress.
        self._suppressed_until: float = 0.0

        # Subscribe to TTS events.
        self._bus.on(TTS_PLAYING, self._on_tts_event)
        logger.info(
            "EchoSuppressor started (holdoff=%.2fs, buffer_flush=%s)",
            self._holdoff_sec,
            self._audio_client is not None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """``True`` while echo suppression is in effect."""
        return time.monotonic() < self._suppressed_until

    @property
    def holdoff_sec(self) -> float:
        """Hold-off period after TTS stops (seconds)."""
        return self._holdoff_sec

    @holdoff_sec.setter
    def holdoff_sec(self, value: float) -> None:
        if value < 0:
            raise ValueError("holdoff_sec must be >= 0")
        self._holdoff_sec = value

    def suppress_for(self, duration_sec: float) -> None:
        """Manually activate suppression for *duration_sec* seconds."""
        self._suppressed_until = time.monotonic() + duration_sec
        logger.debug("Echo suppression activated for %.1fs", duration_sec)

    def stop(self) -> None:
        """Unsubscribe from the event bus."""
        self._bus.off(TTS_PLAYING, self._on_tts_event)
        logger.info("EchoSuppressor stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_tts_event(self, event: Any) -> None:
        """Handle ``TTS_PLAYING`` events from SpeechOutput."""
        if event is None:
            return

        playing = getattr(event, "playing", None)
        if playing is None:
            return

        if playing:
            # TTS started — suppress indefinitely until it stops.
            self._suppressed_until = float("inf")
            logger.debug("Echo suppression ON: TTS playing")
        else:
            # TTS stopped — hold off, then clear.
            self._suppressed_until = time.monotonic() + self._holdoff_sec
            logger.debug(
                "Echo suppression holdoff %.2fs after TTS stop",
                self._holdoff_sec,
            )
            # Flush echo-contaminated audio from the buffer.
            if self._audio_client is not None:
                self._audio_client.clear_buffer()
                logger.debug("AudioClient buffer flushed (echo discard)")
