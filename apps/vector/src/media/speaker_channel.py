"""SpeakerChannel — audio output to Vector's speaker.

Provides methods to play PCM audio or TTS text on Vector's speaker.
Unlike input channels, this is a command-based channel — callers push
data out rather than subscribing to a stream.

Usage::

    speaker = SpeakerChannel(robot)
    speaker.start()
    speaker.say_text("Hello world")
    speaker.play_wav("/tmp/alert.wav")
    speaker.stop()
"""

from __future__ import annotations

import logging
import tempfile
import threading
import wave
from typing import Any

from apps.vector.src.media.channel import MediaChannel

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1


class SpeakerChannel(MediaChannel):
    """Audio output channel for Vector's speaker.

    Args:
        robot: Connected ``anki_vector.Robot`` instance.
    """

    def __init__(self, robot: Any) -> None:
        super().__init__("speaker", ring_size=10)
        self._robot = robot
        self._lock_play = threading.Lock()
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start(self) -> None:
        """Mark channel as available."""
        if self._running:
            return
        super().start()
        logger.info("SpeakerChannel started")

    def stop(self) -> None:
        """Mark channel as stopped."""
        super().stop()
        logger.info("SpeakerChannel stopped")

    def say_text(self, text: str, *, blocking: bool = True) -> None:
        """Play TTS text on Vector's speaker.

        Args:
            text: Text to speak.
            blocking: If True (default), block until playback finishes.
                If False, fire in a background thread.
        """
        if not self._running:
            logger.warning("SpeakerChannel not started")
            return

        if blocking:
            self._say_text_sync(text)
        else:
            t = threading.Thread(
                target=self._say_text_sync,
                args=(text,),
                daemon=True,
                name="speaker-tts",
            )
            t.start()

    def play_pcm(
        self,
        pcm_data: bytes,
        sample_rate: int = SAMPLE_RATE,
        volume: int = 100,
    ) -> None:
        """Play raw PCM audio (S16LE mono) on Vector's speaker.

        Writes to a temp WAV file and streams via SDK.
        """
        if not self._running:
            logger.warning("SpeakerChannel not started")
            return

        if not pcm_data:
            return

        with self._lock_play:
            if self._playing:
                logger.debug("Skipping play_pcm — already playing")
                return
            self._playing = True

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
                with wave.open(tmp, "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm_data)

            self._robot.audio.stream_wav_file(tmp_path, volume=volume)
        except Exception:
            logger.warning("play_pcm failed", exc_info=True)
        finally:
            self._playing = False
            import os
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def play_wav(self, path: str, volume: int = 100) -> None:
        """Play a WAV file on Vector's speaker."""
        if not self._running:
            logger.warning("SpeakerChannel not started")
            return

        try:
            self._robot.audio.stream_wav_file(path, volume=volume)
        except Exception:
            logger.warning("play_wav failed", exc_info=True)

    def get_status(self) -> dict:
        status = super().get_status()
        status["playing"] = self._playing
        return status

    # -- Internal -----------------------------------------------------------

    def _say_text_sync(self, text: str) -> None:
        """Synchronous TTS — blocks until done."""
        with self._lock_play:
            if self._playing:
                logger.debug("Skipping say_text — already playing")
                return
            self._playing = True

        try:
            self._robot.behavior.say_text(text)
            logger.debug("TTS done: %r", text[:50])
        except Exception:
            logger.warning("say_text failed", exc_info=True)
        finally:
            self._playing = False
