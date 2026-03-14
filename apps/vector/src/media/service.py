"""MediaService — manages all Vector media channels on demand.

Provides four channels that any service can start/stop independently:

- **camera** (video in): JPEG frames from Vector's camera → subscribers
- **mic** (audio in): PCM from vector-streamer Opus → subscribers
- **speaker** (audio out): TTS or PCM → Vector speaker
- **display** (video out): PIL images → Vector OLED

Channels are lazy — they only consume resources when started.  Multiple
services can subscribe to the same channel concurrently.

Usage::

    media = MediaService(camera_client=cam, robot=robot, vector_host="192.168.1.73")

    # Any service can start a channel on demand
    media.start_channel("camera")
    sub = media.camera.subscribe()
    jpeg = sub.queue.get(timeout=1)
    sub.close()
    media.stop_channel("camera")

    # Or start all channels at once
    media.start()

    # Speaker and display are push-based
    media.speaker.say_text("Hello")
    media.display.show_text("Hi!", duration=2.0)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apps.vector.src.media.camera_channel import CameraChannel
from apps.vector.src.media.display_channel import DisplayChannel
from apps.vector.src.media.mic_channel import MicChannel
from apps.vector.src.media.speaker_channel import SpeakerChannel

if TYPE_CHECKING:
    from apps.vector.src.camera.camera_client import CameraClient
    from apps.vector.src.media.channel import MediaChannel

logger = logging.getLogger(__name__)


class MediaService:
    """Manages all Vector media channels with on-demand start/stop.

    Args:
        camera_client: CameraClient instance (for camera channel).
        robot: Connected ``anki_vector.Robot`` (for speaker/display).
        vector_host: Vector IP for vector-streamer mic channel.
        vector_port: TCP port for vector-streamer (default 5555).
    """

    def __init__(
        self,
        camera_client: CameraClient | None = None,
        robot: Any = None,
        vector_host: str = "192.168.1.73",
        vector_port: int = 5555,
    ) -> None:
        self._camera: CameraChannel | None = None
        self._mic = MicChannel(host=vector_host, port=vector_port)
        self._speaker: SpeakerChannel | None = None
        self._display: DisplayChannel | None = None

        if camera_client is not None:
            self._camera = CameraChannel(camera_client)
        if robot is not None:
            self._speaker = SpeakerChannel(robot)
            self._display = DisplayChannel(robot)

        self._started = False

    # -- Channel properties -------------------------------------------------

    @property
    def camera(self) -> CameraChannel:
        """Camera video-in channel."""
        if self._camera is None:
            raise RuntimeError("CameraChannel not available — no camera_client provided")
        return self._camera

    @property
    def mic(self) -> MicChannel:
        """Mic audio-in channel."""
        return self._mic

    @property
    def speaker(self) -> SpeakerChannel:
        """Speaker audio-out channel."""
        if self._speaker is None:
            raise RuntimeError("SpeakerChannel not available — no robot provided")
        return self._speaker

    @property
    def display(self) -> DisplayChannel:
        """Display video-out channel."""
        if self._display is None:
            raise RuntimeError("DisplayChannel not available — no robot provided")
        return self._display

    # -- Channel availability -----------------------------------------------

    @property
    def has_camera(self) -> bool:
        return self._camera is not None

    @property
    def has_speaker(self) -> bool:
        return self._speaker is not None

    @property
    def has_display(self) -> bool:
        return self._display is not None

    # -- On-demand start/stop -----------------------------------------------

    def start_channel(self, name: str) -> None:
        """Start a single channel by name.

        Args:
            name: One of "camera", "mic", "speaker", "display".
        """
        ch = self._get_channel(name)
        if ch.is_running:
            logger.debug("Channel %r already running", name)
            return
        ch.start()
        logger.info("Started channel %r on demand", name)

    def stop_channel(self, name: str) -> None:
        """Stop a single channel by name."""
        ch = self._get_channel(name)
        if not ch.is_running:
            logger.debug("Channel %r not running", name)
            return
        ch.stop()
        logger.info("Stopped channel %r", name)

    def start(self) -> None:
        """Start all available channels."""
        if self._started:
            logger.warning("MediaService already started")
            return

        logger.info("Starting MediaService (all channels)...")
        for name, ch in self._all_channels():
            if ch is not None and not ch.is_running:
                ch.start()
        self._started = True
        logger.info("MediaService started")

    def stop(self) -> None:
        """Stop all channels."""
        if not self._started:
            # Stop any individually started channels too
            for name, ch in self._all_channels():
                if ch is not None and ch.is_running:
                    ch.stop()
            return

        logger.info("Stopping MediaService...")
        for name, ch in self._all_channels():
            if ch is not None and ch.is_running:
                ch.stop()
        self._started = False
        logger.info("MediaService stopped")

    def get_status(self) -> dict:
        """Return status of all channels."""
        channels = {}
        for name, ch in self._all_channels():
            if ch is not None:
                channels[name] = ch.get_status()
            else:
                channels[name] = {"available": False}
        return {
            "started": self._started,
            "channels": channels,
        }

    # -- Internal -----------------------------------------------------------

    def _get_channel(self, name: str) -> MediaChannel:
        """Look up a channel by name, raising ValueError if unavailable."""
        mapping: dict[str, MediaChannel | None] = {
            "camera": self._camera,
            "mic": self._mic,
            "speaker": self._speaker,
            "display": self._display,
        }
        if name not in mapping:
            raise ValueError(
                f"Unknown channel {name!r} — valid: {list(mapping.keys())}"
            )
        ch = mapping[name]
        if ch is None:
            raise RuntimeError(f"Channel {name!r} not available (missing dependency)")
        return ch

    def _all_channels(self) -> list[tuple[str, MediaChannel | None]]:
        return [
            ("camera", self._camera),
            ("mic", self._mic),
            ("speaker", self._speaker),
            ("display", self._display),
        ]
