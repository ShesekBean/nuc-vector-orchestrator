"""Hybrid wake word detector: Vector SDK event + openwakeword on NUC.

Two independent backends that both emit ``WAKE_WORD_DETECTED`` on the NUC
event bus:

1. **SDK backend** — subscribes to Vector's onboard ``wake_word`` event.
   Zero NUC latency, limited to "Hey Vector".
2. **openwakeword backend** — runs inference on NUC from AudioClient PCM.
   Supports any custom wake word model (e.g., "hey_jarvis").

Echo suppression prevents TTS audio from re-triggering the wake word.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import TYPE_CHECKING, Any

from apps.vector.src.events.event_types import (
    TTS_PLAYING,
    WAKE_WORD_DETECTED,
    WakeWordDetectedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.voice.audio_client import AudioClient

logger = logging.getLogger(__name__)

# openwakeword expects 16 kHz mono int16 in 80 ms chunks (1280 samples)
OWW_CHUNK_SAMPLES = 1280
OWW_CHUNK_BYTES = OWW_CHUNK_SAMPLES * 2

# Default detection threshold (tuned for R3 USB mic at ~1m)
DEFAULT_THRESHOLD = 0.4

# Minimum seconds between detections (debounce)
DEFAULT_COOLDOWN_SEC = 2.0

# How long to suppress after TTS finishes (prevents echo re-trigger)
DEFAULT_TTS_HOLDOFF_SEC = 1.5


class WakeWordDetector:
    """Hybrid wake word detector with SDK and openwakeword backends.

    Args:
        nuc_bus: NUC event bus for emitting detections and listening to TTS.
        threshold: Confidence threshold for openwakeword detections.
        cooldown_sec: Minimum seconds between detections (debounce).
        tts_holdoff_sec: Seconds to suppress after TTS stops.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        tts_holdoff_sec: float = DEFAULT_TTS_HOLDOFF_SEC,
    ) -> None:
        self._bus = nuc_bus
        self._threshold = threshold
        self._cooldown_sec = cooldown_sec
        self._tts_holdoff_sec = tts_holdoff_sec

        # Suppression state
        self._suppressed_until: float = 0.0
        self._last_detection_time: float = 0.0

        # openwakeword state
        self._oww_model: Any = None
        self._oww_thread: threading.Thread | None = None
        self._oww_stop = threading.Event()
        self._oww_model_names: list[str] = []
        self._audio_client: AudioClient | None = None

        # SDK state
        self._sdk_robot: Any = None
        self._sdk_active = False

        # TTS echo suppression — subscribe to bus
        self._bus.on(TTS_PLAYING, self._on_tts_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_sdk_listener(self, robot: Any) -> None:
        """Subscribe to Vector's built-in wake_word SDK event.

        Args:
            robot: Connected ``anki_vector.Robot`` instance.
        """
        if self._sdk_active:
            logger.warning("SDK wake word listener already active")
            return

        try:
            from anki_vector.events import Events
        except ImportError:
            logger.warning(
                "anki_vector not installed — SDK wake word listener disabled"
            )
            return

        self._sdk_robot = robot
        robot.events.subscribe(self._on_sdk_wake_word, Events.wake_word)
        self._sdk_active = True
        logger.info("SDK wake word listener started")

    def start_oww_listener(
        self,
        audio_client: AudioClient,
        model: str = "hey_jarvis_v0.1",
        *,
        threshold: float | None = None,
    ) -> None:
        """Start openwakeword processing thread.

        Args:
            audio_client: AudioClient providing 16 kHz int16 PCM.
            model: openwakeword model name to load.
            threshold: Override default threshold for this model.
        """
        if self._oww_thread is not None and self._oww_thread.is_alive():
            logger.warning("openwakeword listener already running")
            return

        if threshold is not None:
            self._threshold = threshold

        self._audio_client = audio_client
        self._oww_stop.clear()

        # Load model (lazy import — openwakeword may not be installed)
        try:
            import openwakeword  # noqa: F401
            from openwakeword.model import Model
        except ImportError:
            logger.error(
                "openwakeword not installed — run: pip install openwakeword"
            )
            return

        self._oww_model = Model(wakeword_models=[model])
        self._oww_model_names = list(self._oww_model.prediction_buffer.keys())
        if not self._oww_model_names:
            logger.error("No wake word models loaded from: %s", model)
            return

        logger.info(
            "openwakeword loaded models: %s (threshold=%.2f)",
            self._oww_model_names,
            self._threshold,
        )

        self._oww_thread = threading.Thread(
            target=self._oww_loop, daemon=True, name="oww-detector"
        )
        self._oww_thread.start()

    def stop(self) -> None:
        """Tear down all listeners."""
        # Stop openwakeword thread
        if self._oww_thread is not None:
            self._oww_stop.set()
            self._oww_thread.join(timeout=5.0)
            self._oww_thread = None
            self._oww_model = None
            logger.info("openwakeword listener stopped")

        # Unsubscribe SDK listener
        if self._sdk_active and self._sdk_robot is not None:
            try:
                from anki_vector.events import Events

                self._sdk_robot.events.unsubscribe(
                    self._on_sdk_wake_word, Events.wake_word
                )
            except Exception:
                logger.exception("Error unsubscribing SDK wake word")
            self._sdk_active = False
            self._sdk_robot = None
            logger.info("SDK wake word listener stopped")

        # Unsubscribe TTS listener
        self._bus.off(TTS_PLAYING, self._on_tts_event)

    def suppress_for(self, duration_sec: float) -> None:
        """Suppress detections for *duration_sec* (echo suppression)."""
        self._suppressed_until = time.monotonic() + duration_sec
        logger.debug("Wake word suppressed for %.1fs", duration_sec)

    @property
    def is_suppressed(self) -> bool:
        """True if detections are currently suppressed."""
        return time.monotonic() < self._suppressed_until

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        self._threshold = value

    @property
    def sdk_active(self) -> bool:
        return self._sdk_active

    @property
    def oww_active(self) -> bool:
        return (
            self._oww_thread is not None
            and self._oww_thread.is_alive()
        )

    # ------------------------------------------------------------------
    # Internal: SDK backend
    # ------------------------------------------------------------------

    def _on_sdk_wake_word(self, _robot: Any, _name: str, _msg: Any) -> None:
        """Handle Vector's built-in wake_word SDK event."""
        self._try_emit(
            model="hey_vector_sdk",
            confidence=1.0,
            source="sdk",
        )

    # ------------------------------------------------------------------
    # Internal: openwakeword backend
    # ------------------------------------------------------------------

    def _oww_loop(self) -> None:
        """Background thread: read audio chunks and run openwakeword."""
        logger.info("openwakeword detection loop started")
        residual = b""

        while not self._oww_stop.is_set():
            if self._audio_client is None:
                break

            chunk = self._audio_client.get_latest_chunk()
            if chunk is None:
                self._oww_stop.wait(0.05)
                continue

            # Accumulate PCM into OWW_CHUNK_BYTES-sized pieces
            residual += chunk
            while len(residual) >= OWW_CHUNK_BYTES:
                frame = residual[:OWW_CHUNK_BYTES]
                residual = residual[OWW_CHUNK_BYTES:]
                self._process_oww_frame(frame)

            # Pace the loop — openwakeword runs at ~12.5 fps (80ms chunks)
            self._oww_stop.wait(0.08)

        logger.info("openwakeword detection loop exited")

    def _process_oww_frame(self, pcm_bytes: bytes) -> None:
        """Run openwakeword inference on a single 80ms chunk."""
        if self._oww_model is None:
            return

        # CRITICAL: openwakeword requires int16, NOT float32 (R3 lesson)
        n_samples = len(pcm_bytes) // 2
        audio_int16 = struct.unpack(f"<{n_samples}h", pcm_bytes)

        prediction = self._oww_model.predict(audio_int16)

        for model_name in self._oww_model_names:
            score = prediction.get(model_name, 0.0)
            if score >= self._threshold:
                direction = -1
                if self._audio_client is not None:
                    direction = self._audio_client.source_direction
                self._try_emit(
                    model=model_name,
                    confidence=score,
                    source="openwakeword",
                    source_direction=direction,
                )
                # Reset prediction buffer after detection to prevent
                # repeated triggers on the same utterance
                self._oww_model.reset()
                break

    # ------------------------------------------------------------------
    # Internal: shared emission logic
    # ------------------------------------------------------------------

    def _try_emit(
        self,
        *,
        model: str,
        confidence: float,
        source: str,
        source_direction: int = -1,
    ) -> None:
        """Emit a detection event if not suppressed or in cooldown."""
        now = time.monotonic()

        if now < self._suppressed_until:
            logger.debug(
                "Wake word suppressed (%.1fs remaining)",
                self._suppressed_until - now,
            )
            return

        if now - self._last_detection_time < self._cooldown_sec:
            logger.debug(
                "Wake word in cooldown (%.1fs remaining)",
                self._cooldown_sec - (now - self._last_detection_time),
            )
            return

        self._last_detection_time = now

        event = WakeWordDetectedEvent(
            model=model,
            confidence=confidence,
            source=source,
            source_direction=source_direction,
        )
        self._bus.emit(WAKE_WORD_DETECTED, event)
        logger.info(
            "Wake word detected: model=%s confidence=%.2f source=%s dir=%d",
            model,
            confidence,
            source,
            source_direction,
        )

    # ------------------------------------------------------------------
    # Internal: echo suppression
    # ------------------------------------------------------------------

    def _on_tts_event(self, event: Any) -> None:
        """Auto-suppress when TTS starts, hold off after TTS stops."""
        if event is None:
            return

        playing = getattr(event, "playing", None)
        if playing is None:
            return

        if playing:
            # TTS started — suppress indefinitely (cleared when TTS stops)
            self._suppressed_until = float("inf")
            logger.debug("Wake word suppressed: TTS playing")
        else:
            # TTS stopped — hold off for a bit to let echo decay
            self._suppressed_until = time.monotonic() + self._tts_holdoff_sec
            logger.debug(
                "Wake word holdoff %.1fs after TTS", self._tts_holdoff_sec
            )
