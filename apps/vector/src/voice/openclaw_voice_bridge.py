"""OpenClaw voice bridge — wake word → record → STT → agent → say_text().

Connects Vector's mic audio pipeline to OpenClaw's agent via:
1. Wake word detection triggers recording
2. Silence detection ends recording
3. gpt-4o-transcribe STT (via OpenAI API)
4. OpenClaw hooks/agent endpoint processes the text through skills
5. ``say_text()`` speaks the response through Vector's built-in TTS

All inference and processing runs on NUC; Vector is a thin gRPC client.
"""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import threading
import time
import wave
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    STT_RESULT,
    TTS_PLAYING,
    WAKE_WORD_DETECTED,
    CommandReceivedEvent,
    SttResultEvent,
    TtsPlayingEvent,
)
from apps.vector.src.voice.speech_output import SpeechOutput

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.voice.audio_client import AudioClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (all overridable via constructor or env vars)
# ---------------------------------------------------------------------------

DEFAULT_HOOKS_URL = "http://127.0.0.1:18889/hooks/agent"
DEFAULT_HOOKS_TOKEN_PATH = str(Path.home() / ".openclaw" / "hooks-token")
DEFAULT_MAX_LISTEN_SEC = 10.0
DEFAULT_SILENCE_THRESHOLD = 300  # RMS energy below this = silence
DEFAULT_SILENCE_DURATION_SEC = 1.5  # silence this long ends recording
DEFAULT_MAX_RESPONSE_CHARS = 300  # truncate for say_text() TTS
DEFAULT_HOOKS_TIMEOUT_SEC = 30  # timeout for OpenClaw agent response
DEFAULT_STT_TIMEOUT_SEC = 15  # timeout for OpenAI transcription
DEFAULT_LISTEN_POLL_INTERVAL = 0.1  # seconds between silence checks


class BridgeState(Enum):
    """Voice bridge state machine."""

    IDLE = auto()
    LISTENING = auto()
    TRANSCRIBING = auto()
    PROCESSING = auto()
    SPEAKING = auto()


def _rms_energy(pcm_int16: bytes) -> float:
    """Compute RMS energy of 16-bit PCM audio."""
    n_samples = len(pcm_int16) // 2
    if n_samples == 0:
        return 0.0
    samples = struct.unpack(f"<{n_samples}h", pcm_int16)
    sum_sq = sum(s * s for s in samples)
    return (sum_sq / n_samples) ** 0.5


def _pcm_to_wav(pcm: bytes, sample_rate: int = 16_000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container (in-memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _transcribe_openai(wav_bytes: bytes, api_key: str,
                       timeout: float = DEFAULT_STT_TIMEOUT_SEC) -> str:
    """Call OpenAI gpt-4o-transcribe API with WAV audio.

    Uses urllib only — no external HTTP library required.
    Returns the transcribed text, or empty string on failure.
    """
    boundary = "----VoiceBridgeBoundary9876543210"
    body_parts: list[bytes] = []

    # model field
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        b"Content-Disposition: form-data; name=\"model\"\r\n\r\n"
    )
    body_parts.append(b"gpt-4o-transcribe\r\n")

    # file field
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        b"Content-Disposition: form-data; name=\"file\"; "
        b"filename=\"recording.wav\"\r\n"
    )
    body_parts.append(b"Content-Type: audio/wav\r\n\r\n")
    body_parts.append(wav_bytes)
    body_parts.append(b"\r\n")

    # language hint
    body_parts.append(f"--{boundary}\r\n".encode())
    body_parts.append(
        b"Content-Disposition: form-data; name=\"language\"\r\n\r\n"
    )
    body_parts.append(b"en\r\n")

    # closing boundary
    body_parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(body_parts)

    req = Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("text", "").strip()
            logger.info("STT result: '%s'", text[:80])
            return text
    except HTTPError as exc:
        logger.error("OpenAI STT HTTP error %d: %s", exc.code, exc.reason)
    except URLError as exc:
        logger.error("OpenAI STT connection error: %s", exc.reason)
    except Exception:
        logger.exception("OpenAI STT unexpected error")
    return ""


def _send_to_openclaw(text: str, hooks_url: str, hooks_token: str,
                      timeout: float = DEFAULT_HOOKS_TIMEOUT_SEC) -> str:
    """Send transcribed text to OpenClaw hooks/agent endpoint.

    Returns the agent's response text, or empty string on failure.
    """
    payload = json.dumps({
        "message": text,
        "deliver": False,
    }).encode("utf-8")

    req = Request(hooks_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {hooks_token}")

    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            response = data.get("response", data.get("text", ""))
            if isinstance(response, str):
                response = response.strip()
            else:
                response = str(response)
            logger.info(
                "OpenClaw response (%.0f chars): '%s'",
                len(response),
                response[:80],
            )
            return response
    except HTTPError as exc:
        logger.error(
            "OpenClaw hooks HTTP error %d: %s", exc.code, exc.reason
        )
    except URLError as exc:
        logger.error("OpenClaw hooks connection error: %s", exc.reason)
    except Exception:
        logger.exception("OpenClaw hooks unexpected error")
    return ""


class OpenClawVoiceBridge:
    """Bridges Vector's mic audio to OpenClaw agent via voice.

    Args:
        nuc_bus: NUC event bus for event pub/sub.
        audio_client: AudioClient providing 16 kHz int16 PCM.
        robot: Connected ``anki_vector.Robot`` instance (for say_text).
        hooks_url: OpenClaw hooks endpoint URL.
        hooks_token: Bearer token for hooks auth (read from file if path).
        openai_api_key: OpenAI API key for gpt-4o-transcribe.
        max_listen_sec: Maximum recording duration after wake word.
        silence_threshold: RMS energy below which audio is considered silent.
        silence_duration_sec: Seconds of silence to end recording.
        max_response_chars: Truncate agent response for TTS.
    """

    def __init__(
        self,
        nuc_bus: NucEventBus,
        audio_client: AudioClient,
        robot: Any = None,
        *,
        hooks_url: str | None = None,
        hooks_token: str | None = None,
        openai_api_key: str | None = None,
        max_listen_sec: float = DEFAULT_MAX_LISTEN_SEC,
        silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
        silence_duration_sec: float = DEFAULT_SILENCE_DURATION_SEC,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
    ) -> None:
        self._bus = nuc_bus
        self._audio_client = audio_client
        self._robot = robot

        # Configuration
        self._hooks_url = hooks_url or os.getenv(
            "OPENCLAW_HOOKS_URL", DEFAULT_HOOKS_URL
        )
        self._hooks_token = hooks_token or self._load_hooks_token()
        self._openai_api_key = openai_api_key or os.getenv(
            "OPENAI_API_KEY", ""
        )
        self._max_listen_sec = max_listen_sec
        self._silence_threshold = silence_threshold
        self._silence_duration_sec = silence_duration_sec
        self._max_response_chars = max_response_chars

        # Speech output (delegates say_text + chunking + events)
        self._speech = SpeechOutput(
            nuc_bus, robot, max_chunk_chars=max_response_chars
        )

        # State machine
        self._state = BridgeState.IDLE
        self._state_lock = threading.Lock()

        # Processing thread
        self._thread: threading.Thread | None = None

        # Metrics
        self._total_interactions = 0
        self._total_errors = 0
        self._last_latency_ms: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to wake word events and begin listening."""
        self._bus.on(WAKE_WORD_DETECTED, self._on_wake_word)
        logger.info(
            "Voice bridge started (hooks=%s, stt=%s)",
            self._hooks_url,
            "gpt-4o-transcribe" if self._openai_api_key else "UNCONFIGURED",
        )

    def stop(self) -> None:
        """Unsubscribe from events and clean up."""
        self._bus.off(WAKE_WORD_DETECTED, self._on_wake_word)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info(
            "Voice bridge stopped (interactions=%d, errors=%d)",
            self._total_interactions,
            self._total_errors,
        )

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def total_interactions(self) -> int:
        return self._total_interactions

    @property
    def total_errors(self) -> int:
        return self._total_errors

    @property
    def last_latency_ms(self) -> float:
        return self._last_latency_ms

    # ------------------------------------------------------------------
    # Internal: wake word handler
    # ------------------------------------------------------------------

    def _on_wake_word(self, event: Any) -> None:
        """Handle wake word detection — start voice interaction pipeline."""
        with self._state_lock:
            if self._state != BridgeState.IDLE:
                logger.debug(
                    "Wake word ignored — bridge in state %s", self._state.name
                )
                return
            self._state = BridgeState.LISTENING

        model = getattr(event, "model", "unknown")
        logger.info("Wake word received (model=%s) — starting interaction", model)

        self._thread = threading.Thread(
            target=self._interaction_loop,
            daemon=True,
            name="voice-bridge-interaction",
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Internal: main interaction pipeline
    # ------------------------------------------------------------------

    def _interaction_loop(self) -> None:
        """Run the full voice interaction: listen → STT → agent → TTS."""
        start_time = time.monotonic()
        try:
            self._total_interactions += 1

            # Step 1: Record speech with silence detection
            pcm = self._record_speech()
            if not pcm:
                logger.warning("No speech recorded — returning to idle")
                self._total_errors += 1
                return

            # Step 2: Transcribe via gpt-4o-transcribe
            with self._state_lock:
                self._state = BridgeState.TRANSCRIBING

            text = self._transcribe(pcm)
            if not text:
                logger.warning("STT returned empty — returning to idle")
                self._total_errors += 1
                return

            self._bus.emit(
                STT_RESULT,
                SttResultEvent(text=text, confidence=1.0, language="en"),
            )

            # Step 3: Send to OpenClaw agent
            with self._state_lock:
                self._state = BridgeState.PROCESSING

            self._bus.emit(
                COMMAND_RECEIVED,
                CommandReceivedEvent(
                    command=text, source="voice", args={}
                ),
            )

            response = self._query_openclaw(text)
            if not response:
                logger.warning("OpenClaw returned empty — returning to idle")
                self._total_errors += 1
                return

            # Step 4: Speak response via say_text()
            with self._state_lock:
                self._state = BridgeState.SPEAKING

            self._speak(response)

            elapsed_ms = (time.monotonic() - start_time) * 1000
            self._last_latency_ms = elapsed_ms
            logger.info(
                "Voice interaction complete (%.0fms): '%s' → '%s'",
                elapsed_ms,
                text[:50],
                response[:50],
            )

        except Exception:
            logger.exception("Voice interaction failed")
            self._total_errors += 1
        finally:
            with self._state_lock:
                self._state = BridgeState.IDLE

    # ------------------------------------------------------------------
    # Internal: speech recording with silence detection
    # ------------------------------------------------------------------

    def _record_speech(self) -> bytes:
        """Record audio from AudioClient until silence or timeout.

        Returns raw PCM bytes (16 kHz, 16-bit, mono).
        """
        chunks: list[bytes] = []
        silence_start: float | None = None
        deadline = time.monotonic() + self._max_listen_sec
        last_chunk_id = self._audio_client.chunk_count

        logger.debug(
            "Recording started (max=%.1fs, silence_thresh=%d, silence_dur=%.1fs)",
            self._max_listen_sec,
            self._silence_threshold,
            self._silence_duration_sec,
        )

        while time.monotonic() < deadline:
            current_id = self._audio_client.chunk_count
            if current_id == last_chunk_id:
                time.sleep(DEFAULT_LISTEN_POLL_INTERVAL)
                continue

            chunk = self._audio_client.get_latest_chunk()
            if chunk is None:
                time.sleep(DEFAULT_LISTEN_POLL_INTERVAL)
                continue

            last_chunk_id = current_id
            chunks.append(chunk)

            energy = _rms_energy(chunk)

            if energy < self._silence_threshold:
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start >= self._silence_duration_sec:
                    logger.debug(
                        "Silence detected after %.1fs — stopping recording",
                        time.monotonic() - (deadline - self._max_listen_sec),
                    )
                    break
            else:
                silence_start = None

            time.sleep(DEFAULT_LISTEN_POLL_INTERVAL)

        pcm = b"".join(chunks)
        duration = (len(pcm) / 2) / 16_000
        logger.info("Recorded %.1fs of audio (%d bytes)", duration, len(pcm))
        return pcm

    # ------------------------------------------------------------------
    # Internal: STT
    # ------------------------------------------------------------------

    def _transcribe(self, pcm: bytes) -> str:
        """Transcribe PCM audio via OpenAI gpt-4o-transcribe."""
        if not self._openai_api_key:
            logger.error("OPENAI_API_KEY not set — cannot transcribe")
            return ""

        wav_bytes = _pcm_to_wav(pcm)
        return _transcribe_openai(wav_bytes, self._openai_api_key)

    # ------------------------------------------------------------------
    # Internal: OpenClaw agent query
    # ------------------------------------------------------------------

    def _query_openclaw(self, text: str) -> str:
        """Send text to OpenClaw agent and return response."""
        if not self._hooks_token:
            logger.error("OpenClaw hooks token not configured")
            return ""

        return _send_to_openclaw(
            text, self._hooks_url, self._hooks_token
        )

    # ------------------------------------------------------------------
    # Internal: TTS via SpeechOutput
    # ------------------------------------------------------------------

    def _speak(self, text: str) -> None:
        """Speak text through Vector's built-in TTS via SpeechOutput."""
        self._speech.speak(text)

    # ------------------------------------------------------------------
    # Internal: config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_hooks_token() -> str:
        """Load hooks token from file or env var."""
        token_path = os.getenv(
            "OPENCLAW_HOOKS_TOKEN_PATH", DEFAULT_HOOKS_TOKEN_PATH
        )
        try:
            return Path(token_path).read_text().strip()
        except FileNotFoundError:
            logger.debug("Hooks token file not found: %s", token_path)
        except OSError as exc:
            logger.warning("Cannot read hooks token: %s", exc)

        return os.getenv("OPENCLAW_HOOKS_TOKEN", "")
