"""Intercom — send text messages and photos to Ophir via Signal.

Communicates with the bridge server's /intercom/* endpoints which handle
Signal delivery via the openclaw-gateway container (JSON-RPC).

Photo capture is delegated to the bridge server, which grabs the latest
camera frame and sends it as a Signal attachment.

Usage:
    from apps.vector.src.events.nuc_event_bus import NucEventBus
    from apps.vector.src.intercom import Intercom

    bus = NucEventBus()
    intercom = Intercom(event_bus=bus)
    intercom.send_text("Hello from Vector!")
    intercom.send_photo("Here's what I see")
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import Request, urlopen

from apps.vector.src.events.event_types import (
    COMMAND_RECEIVED,
    INTERCOM_PHOTO_SENT,
    INTERCOM_TEXT_SENT,
    IntercomPhotoSentEvent,
    IntercomTextSentEvent,
)

if TYPE_CHECKING:
    from apps.vector.src.events.event_types import CommandReceivedEvent
    from apps.vector.src.events.nuc_event_bus import NucEventBus

logger = logging.getLogger(__name__)

DEFAULT_INTERCOM_URL = "http://127.0.0.1:8081"
_TIMEOUT_SECONDS = 10


class Intercom:
    """Client for the NUC intercom server — sends text and photos to Signal."""

    def __init__(
        self,
        event_bus: NucEventBus | None = None,
        intercom_url: str | None = None,
    ) -> None:
        self._bus = event_bus
        self._base_url = (
            intercom_url
            or os.getenv("INTERCOM_URL", DEFAULT_INTERCOM_URL)
        ).rstrip("/")

        if self._bus is not None:
            self._bus.on(COMMAND_RECEIVED, self._on_command)

    # --- Public API ----------------------------------------------------------

    def send_text(self, text: str) -> bool:
        """Send a text message to Ophir via Signal.

        Returns True if the intercom server accepted the message.
        """
        if not text.strip():
            logger.warning("Ignoring empty intercom text")
            return False

        payload = {"text": text.strip()}
        success = self._post("/intercom/receive", payload)

        if self._bus is not None:
            self._bus.emit(
                INTERCOM_TEXT_SENT,
                IntercomTextSentEvent(text=text.strip(), success=success),
            )
        return success

    def send_photo(self, caption: str = "Photo from robot") -> bool:
        """Send a camera photo to Ophir via Signal.

        The intercom server fetches the photo from the bridge /capture
        endpoint, so no image data needs to be sent from here.

        Returns True if the intercom server accepted the request.
        """
        effective_caption = caption.strip() or "Photo from robot"
        payload = {"caption": effective_caption}
        success = self._post("/intercom/photo", payload)

        if self._bus is not None:
            self._bus.emit(
                INTERCOM_PHOTO_SENT,
                IntercomPhotoSentEvent(caption=effective_caption, success=success),
            )
        return success

    def health_check(self) -> bool:
        """Check if the intercom server is reachable."""
        try:
            req = Request(f"{self._base_url}/health", method="GET")
            with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                return resp.status == 200
        except (URLError, OSError):
            return False

    # --- Event handler -------------------------------------------------------

    def _on_command(self, event: CommandReceivedEvent) -> None:
        """Handle COMMAND_RECEIVED events for intercom commands."""
        cmd = event.command.lower()
        if cmd in ("take_photo", "take a photo", "photo"):
            caption = event.args.get("caption", "Photo from robot")
            logger.info("Intercom: photo command received (source=%s)", event.source)
            self.send_photo(caption)
        elif cmd in ("intercom", "send_message", "message"):
            text = event.args.get("text", "")
            if text:
                logger.info("Intercom: text command received (source=%s)", event.source)
                self.send_text(text)

    # --- Internal ------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> bool:
        """POST JSON to the intercom server. Returns True on 2xx response."""
        url = f"{self._base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                if resp.status < 300:
                    logger.info("Intercom POST %s → %d", path, resp.status)
                    return True
                logger.warning("Intercom POST %s → %d", path, resp.status)
                return False
        except URLError as exc:
            logger.error("Intercom POST %s failed: %s", path, exc)
            return False
        except OSError as exc:
            logger.error("Intercom POST %s error: %s", path, exc)
            return False
