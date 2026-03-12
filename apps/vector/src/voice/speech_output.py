"""Speech output via Vector's built-in say_text() TTS.

Provides sentence-aware chunking for long responses and volume control.
All processing runs on NUC; ``say_text()`` is a blocking SDK call that
plays audio through Vector's onboard speaker.

Architecture (from issue #21):
    OpenClaw agent → text response → SpeechOutput.speak() → Vector speaker
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import TTS_PLAYING, TtsPlayingEvent

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

# Sentence-ending punctuation followed by whitespace (split points).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Volume level names → SDK enum member names (mapped at call time via lazy import).
_VOLUME_LEVELS = ("mute", "low", "medium_low", "medium", "medium_high", "high")

DEFAULT_MAX_CHUNK_CHARS = 200


class SpeechOutput:
    """Speak text through Vector's built-in TTS with chunking and volume control.

    Args:
        nuc_bus: NUC event bus for ``TTS_PLAYING`` event emission.
        robot: Connected ``anki_vector.Robot`` instance.
        max_chunk_chars: Maximum characters per ``say_text()`` call.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        robot: Any = None,
        *,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    ) -> None:
        self._bus = nuc_bus
        self._robot = robot
        self._max_chunk_chars = max_chunk_chars
        self._volume: str = "medium"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str) -> None:
        """Speak *text* through Vector, chunking if necessary.

        Emits ``TTS_PLAYING(playing=True)`` before the first chunk and
        ``TTS_PLAYING(playing=False)`` after the last chunk (even on error).
        """
        text = text.strip()
        if not text:
            return

        if self._robot is None:
            logger.warning("No robot connected — cannot speak")
            return

        chunks = self.chunk_text(text, self._max_chunk_chars)
        logger.info(
            "Speaking %d chunk(s), %d chars total",
            len(chunks),
            len(text),
        )

        self._bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text=text))
        try:
            for i, chunk in enumerate(chunks):
                logger.debug("Chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
                self._robot.behavior.say_text(chunk)
        except Exception:
            logger.exception("say_text() failed on chunk %d/%d", i + 1, len(chunks))
        finally:
            self._bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text=text))

    def set_volume(self, level: str) -> None:
        """Set Vector's master volume.

        Args:
            level: One of ``"mute"``, ``"low"``, ``"medium_low"``,
                   ``"medium"``, ``"medium_high"``, ``"high"``.
        """
        level = level.strip().lower()
        if level not in _VOLUME_LEVELS:
            logger.error(
                "Invalid volume level '%s' — expected one of %s",
                level,
                _VOLUME_LEVELS,
            )
            return

        if self._robot is None:
            logger.warning("No robot connected — cannot set volume")
            return

        try:
            # Lazy import for CI compatibility (lesson from issue #5).
            from anki_vector.audio import RobotVolumeLevel  # type: ignore[import-untyped]

            enum_name = level.upper()
            vol = RobotVolumeLevel[enum_name]
            self._robot.audio.set_master_volume(vol)
            self._volume = level
            logger.info("Volume set to %s", level)
        except Exception:
            logger.exception("Failed to set volume to '%s'", level)

    @property
    def volume(self) -> str:
        """Current volume level name."""
        return self._volume

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
        """Split *text* into chunks of at most *max_chars* characters.

        Strategy:
        1. Split on sentence boundaries (``., !, ?`` followed by whitespace).
        2. If a sentence still exceeds *max_chars*, split on whitespace.
        3. If a single word exceeds *max_chars*, keep it as-is (say_text
           will handle or truncate internally).

        Returns a non-empty list (at least ``[""]`` for empty input after strip).
        """
        text = text.strip()
        if not text:
            return [""]

        if len(text) <= max_chars:
            return [text]

        sentences = _SENTENCE_RE.split(text)
        chunks: list[str] = []
        current = ""

        for sentence in sentences:
            candidate = f"{current} {sentence}".strip() if current else sentence

            if len(candidate) <= max_chars:
                current = candidate
                continue

            # Flush current if non-empty.
            if current:
                chunks.append(current)
                current = ""

            # If the sentence itself fits, start a new chunk with it.
            if len(sentence) <= max_chars:
                current = sentence
                continue

            # Sentence too long — split on whitespace.
            words = sentence.split()
            for word in words:
                if not current:
                    current = word
                elif len(current) + 1 + len(word) <= max_chars:
                    current = f"{current} {word}"
                else:
                    chunks.append(current)
                    current = word

        if current:
            chunks.append(current)

        return chunks if chunks else [""]
