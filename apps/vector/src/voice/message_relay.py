"""Message relay — detect "tell Ophir" voice commands and send via Signal.

Intercepts STT transcriptions matching relay patterns (e.g., "tell Ophir
I'll be late") and sends the extracted message body to Ophir via the
Intercom module. Confirmation is spoken through Vector's TTS.

This runs on NUC as part of the voice pipeline. No robot gRPC needed.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from apps.vector.src.events.event_types import (
    MESSAGE_RELAYED,
    MessageRelayedEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.intercom import Intercom
    from apps.vector.src.voice.speech_output import SpeechOutput

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Relay patterns — anchored to the start of the transcription.
# Each pattern captures the message body in group 1.
# ---------------------------------------------------------------------------

_RELAY_PATTERNS: list[re.Pattern[str]] = [
    # "tell Ophir ..." / "tell Ophir that ..."
    re.compile(
        r"^(?:hey\s+vector[,.]?\s+)?tell\s+ophir\s+(?:that\s+)?(.+)",
        re.IGNORECASE,
    ),
    # "message Ophir ..."
    re.compile(
        r"^(?:hey\s+vector[,.]?\s+)?message\s+ophir\s+(.+)",
        re.IGNORECASE,
    ),
    # "let Ophir know ..." / "let Ophir know that ..."
    re.compile(
        r"^(?:hey\s+vector[,.]?\s+)?let\s+ophir\s+know\s+(?:that\s+)?(.+)",
        re.IGNORECASE,
    ),
    # "send Ophir a message ..."  / "send a message to Ophir ..."
    re.compile(
        r"^(?:hey\s+vector[,.]?\s+)?send\s+ophir\s+a\s+message\s+(?:saying\s+)?(.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:hey\s+vector[,.]?\s+)?send\s+a\s+message\s+to\s+ophir\s+(?:saying\s+)?(.+)",
        re.IGNORECASE,
    ),
]

_CONFIRM_SUCCESS = "Message sent to Ophir."
_CONFIRM_FAILURE = "Sorry, I couldn't send that message. The intercom server may be offline."


def extract_relay_message(text: str) -> str | None:
    """Extract the message body from a relay command.

    Returns the message body if *text* matches a relay pattern, else ``None``.
    """
    text = text.strip()
    if not text:
        return None

    for pattern in _RELAY_PATTERNS:
        match = pattern.match(text)
        if match:
            body = match.group(1).strip().rstrip(".")
            return body if body else None
    return None


class MessageRelay:
    """Relay "tell Ophir ..." voice commands to Signal via Intercom.

    Args:
        intercom: Intercom client for sending Signal messages.
        speech: SpeechOutput for TTS confirmation (optional — skipped in tests).
        nuc_bus: Event bus for emitting MESSAGE_RELAYED events (optional).
    """

    def __init__(
        self,
        intercom: Intercom,
        speech: SpeechOutput | None = None,
        nuc_bus: NucEventBus | None = None,
    ) -> None:
        self._intercom = intercom
        self._speech = speech
        self._bus = nuc_bus

    def try_relay(self, text: str) -> str | None:
        """Attempt to relay a "tell Ophir" message.

        Returns confirmation text if the message was handled (caller should
        speak it and skip further processing), or ``None`` if the text does
        not match any relay pattern.
        """
        body = extract_relay_message(text)
        if body is None:
            return None

        logger.info("Relay detected — sending to Ophir: '%s'", body[:80])

        success = self._intercom.send_text(body)
        confirmation = _CONFIRM_SUCCESS if success else _CONFIRM_FAILURE

        if self._bus is not None:
            self._bus.emit(
                MESSAGE_RELAYED,
                MessageRelayedEvent(
                    original_text=text.strip(),
                    extracted_message=body,
                    success=success,
                ),
            )

        if self._speech is not None:
            self._speech.speak(confirmation)

        return confirmation
