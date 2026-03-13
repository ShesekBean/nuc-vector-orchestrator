"""MediaService -- singleton managing all media channels.

Currently provides:
- ``mic``: MicChannel (vector-streamer Opus -> PCM)

Future channels: camera (direct H264), speaker (PCM -> vector-streamer).

Usage::

    media = MediaService(vector_host="192.168.1.73")
    media.start()
    ...
    sub = media.mic.subscribe()
    pcm = sub.queue.get(timeout=1)
    ...
    media.stop()
"""

from __future__ import annotations

import logging

from apps.vector.src.media.mic_channel import MicChannel

logger = logging.getLogger(__name__)


class MediaService:
    """Manages all Vector media channels.

    Args:
        vector_host: Vector IP address for connecting to vector-streamer.
        vector_port: TCP port for vector-streamer (default 5555).
    """

    def __init__(
        self,
        vector_host: str = "192.168.1.73",
        vector_port: int = 5555,
    ) -> None:
        self._mic = MicChannel(host=vector_host, port=vector_port)
        self._started = False

    @property
    def mic(self) -> MicChannel:
        """The mic audio channel."""
        return self._mic

    def start(self) -> None:
        """Start all media channels."""
        if self._started:
            logger.warning("MediaService already started")
            return

        logger.info("Starting MediaService...")
        self._mic.start()
        self._started = True
        logger.info("MediaService started")

    def stop(self) -> None:
        """Stop all media channels."""
        if not self._started:
            return

        logger.info("Stopping MediaService...")
        self._mic.stop()
        self._started = False
        logger.info("MediaService stopped")

    def get_status(self) -> dict:
        """Return status of all channels."""
        return {
            "started": self._started,
            "channels": {
                "mic": self._mic.get_status(),
            },
        }
